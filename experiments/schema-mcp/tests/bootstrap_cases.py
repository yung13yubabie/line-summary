"""New real-stdio cases. This client never opens DB/settings or extracts keys."""
import json
from pathlib import Path
import time


def read_object(path):
    raw = Path(path).read_bytes()
    if len(raw) > 16384:
        raise ValueError('Bounded local observation required')
    return json.loads(raw)


def ready(server, out):
    # Synchronize with the unchanged native stage budget (25s); this only waits
    # for an injected fault and never extends any provider/launcher deadline.
    deadline = min(server.deadline, time.monotonic() + 27)
    while time.monotonic() < deadline:
        server.drain()
        paths = list((out / 'artifacts').rglob('timeout_ready.json'))
        if len(paths) == 1 and read_object(paths[0]) == {'candidate_loop_checked': True}:
            return
        if len(paths) > 1 or server.child.poll() is not None:
            raise ValueError('Synthetic loop marker rejected')
        time.sleep(.005)
    raise TimeoutError('Synthetic candidate loop not reached')


def no_source(out):
    return not list((out / 'artifacts').rglob('configured_backend_started.json'))


def run_case(server, out, mode, protocol, checks=None, results=None):
    checks = {} if checks is None else checks
    results = [] if results is None else results
    started = time.monotonic()
    server.open()
    # This is the first byte/frame sent; there is no fixture/private preamble.
    initialized = server.exchange(1, 'initialize', {'protocolVersion': '2025-11-25',
        'capabilities': {}, 'clientInfo': {'name': 'synthetic-bootstrap-client', 'version': '1'}}, seconds=10)
    checks['first_frame_initialize'] = initialized['protocolVersion'] == '2025-11-25'
    checks['startup_seconds'] = time.monotonic() - started
    server.notify('notifications/initialized')
    listing = server.exchange(2, 'tools/list', {})
    checks['one_tool_empty_schema'] = (len(listing['tools']) == 1
        and listing['tools'][0]['name'] == 'line_schema_counts'
        and listing['tools'][0]['inputSchema'] == {'type': 'object', 'properties': {}, 'additionalProperties': False})
    checks['handshake_did_not_create_source'] = no_source(out)

    def call(identifier, arguments=None):
        result = server.exchange(identifier, 'tools/call',
            {'name': 'line_schema_counts', 'arguments': {} if arguments is None else arguments}, seconds=35)
        return protocol.require_schema_result(result)

    if mode == 'eof_idle':
        return checks, results
    if mode == 'basic':
        identifier = 3
        for name, arguments in [('line_schema_counts', {'settings_path': 'MCP_TRANSPORT_PRIVATE_SENTINEL'}),
                                ('line_schema_counts', {'key': 'MCP_TRANSPORT_PRIVATE_SENTINEL'}),
                                ('line_list_chats', {}), ('line_get_history', {}),
                                ('line_get_unread', {}), ('line_get_contacts', {})]:
            value = server.exchange(identifier, 'tools/call', {'name': name, 'arguments': arguments})
            assert value.get('isError') is True and 'structuredContent' not in value
            assert all(block.get('text') == 'LINE tool request failed' for block in value['content'])
            identifier += 1
        checks['rejections_precede_source'] = no_source(out)
        server.send(b'{"MCP_TRANSPORT_PRIVATE_SENTINEL":invalid}\n')
        server.diagnostic()
        checks['malformed_then_ping'] = server.exchange(identifier, 'ping', {}) == {}
        results.append(call(identifier + 1))
    elif mode in ('cancel', 'eof_active', 'cancel_then_success'):
        server.send(protocol.request('tools/call', 3, {'name': 'line_schema_counts', 'arguments': {}}))
        ready(server, out)
        checks['candidate_loop_reached'] = True
        if mode != 'eof_active':
            checks['singleflight_busy'] = call(4)['status'] == 'busy'
            server.notify('notifications/cancelled', {'requestId': 3, 'reason': 'synthetic cancellation'})
            cancelled = server.receive(3, seconds=5, allow_error=True)
            checks['cancelled_without_success'] = 'error' in cancelled and 'result' not in cancelled
            if mode == 'cancel_then_success':
                # Polling only the exact local fixture completion; no PID lookup.
                deadline = time.monotonic() + 3.05
                while time.monotonic() < deadline:
                    server.drain()
                    if list((out / 'artifacts').rglob('fixture_observation.json')):
                        break
                    time.sleep(.005)
                # Cancellation may return before the exact native owner has
                # finished its existing cleanup. Busy is an honest transient.
                deadline = min(server.deadline, time.monotonic() + 3.05)
                value = call(5)
                identifier = 6
                while value['status'] == 'busy' and time.monotonic() < deadline and identifier < 38:
                    time.sleep(.05)
                    value = call(identifier)
                    identifier += 1
                results.append(value)
    elif mode == 'cross_request':
        results.extend((call(3), call(4)))
    else:
        results.append(call(3))
    if mode in ('basic', 'cross_request', 'cancel_then_success'):
        checks['actual_wal_aggregate_counts'] = bool(results) and all(value == {
            'status': 'completed', 'successful': True, 'application_ack': True,
            'os_release_confirmed': True, 'retry_required': False,
            'counts': {'table_count': 2, 'view_count': 1}} for value in results)
    elif mode not in ('cancel', 'eof_active', 'eof_idle'):
        # application_ack acknowledges a terminal application receipt. A known
        # failed source can acknowledge its failure; it never supplies counts.
        expected_status, expected_ack = {
            'wrong_key': ('cleanup_unconfirmed', False),
            'exception': ('cleanup_unconfirmed', False),
            'missing_wal': ('failed', True), 'missing_shm': ('failed', True),
            'invalid_db': ('failed', True), 'timeout': ('cleanup_unconfirmed', False),
        }[mode]
        checks['exact_failure_contract_without_counts'] = bool(results) and all(value == {
            'status': expected_status, 'successful': False, 'application_ack': expected_ack,
            'os_release_confirmed': True, 'retry_required': False} for value in results)
    return checks, results


def after_close(server, out, mode):
    checks = {'server_no_force': server.forced is False, 'server_exact_closed': server.closed is True,
              'server_exit_zero': server.child is not None and server.child.returncode == 0}
    finals = list((out / 'artifacts').rglob('server_lifecycle.json'))
    checks['final_diagnostics_healthy'] = False
    if len(finals) == 1:
        final = read_object(finals[0])
        events = final.get('events') if type(final) is dict else None
        checks['final_diagnostics_healthy'] = (type(events) is list and 0 < len(events) <= 64
            and type(events[-1]) is dict and events[-1].get('phase') == 'server_atexit'
            and type(final.get('emit_failures')) is int and final['emit_failures'] == 0
            and type(final.get('dropped')) is int and final['dropped'] == 0)
    observations = list((out / 'artifacts').rglob('server_observation.json'))
    checks['one_server_observation'] = len(observations) == 1
    if len(observations) == 1:
        value = read_object(observations[0])
        service, guard = value['service'], value['guard']
        checks.update(ordinary_stdio_no_private_frame=value['outer_private_start_frame'] is False,
            terminal_cleanup=value.get('terminal_cleanup_confirmed') is True,
            service_no_pending=service['active_operations'] == 0 and service['pending_cleanup'] is False,
            no_retained_launcher=value['retained_launchers'] == 0,
            wrapper_no_error=value['wrapper_error'] is None,
            diagnostic_sink_healthy=value.get('diagnostic_emit_failures') == 0,
            no_native_memory=guard['blocked_native_memory'] == 0,
            no_external_network=guard['blocked_socket'] == 0,
            no_arbitrary_process=guard['blocked_subprocess'] == 0,
            no_arbitrary_file=guard['blocked_file'] == 0)
        if mode in ('cancel', 'eof_active', 'cancel_then_success'):
            checks['cancellation_reached_service'] = service['cancel_signalled'] is True
    fixtures = list((out / 'artifacts').rglob('fixture_observation.json'))
    expected = 0 if mode == 'eof_idle' else 2 if mode in ('cross_request', 'cancel_then_success') else 1
    checks['expected_fresh_source_count'] = len(fixtures) == expected
    rows = [read_object(path) for path in fixtures]
    checks['all_sources_unchanged'] = all(row.get('source_bytes_unchanged') is True for row in rows)
    checks['all_writers_closed'] = all(row.get('fixture_writer_closed') is True for row in rows)
    checks['all_native_readers_released'] = all(row.get('all_reader_release_confirmed') is True for row in rows)
    checks['no_true_identity_or_memory'] = all(row.get('calls', {}).get('native_memory_calls') == 0 for row in rows)
    checks['real_provider_factory_reached'] = all(row.get('calls', {}).get('factory') == 1 for row in rows)
    checks['all_settings_bound_to_owned_fixture'] = all(row.get('settings_target_verified') is True for row in rows)
    checks['normal_key_path_and_serial_readers'] = all(row.get('normal_key_loader') is True
        and row.get('reader_serial_confirmed') is True and row.get('max_live_readers') == 1
        and row.get('wrapper_error_kind') is None and row.get('LINE_read') is False for row in rows)
    gates = [read_object(path) for path in (out / 'artifacts').rglob('synthetic_gate_observation.json')]
    checks['fixtures_after_native_owned_start'] = len(gates) == expected and all(
        row.get('after_owned_start') is True and row.get('fixed_worker_launches') == 1
        and row.get('LINE_read') is False for row in gates)
    if mode not in ('invalid_db', 'eof_idle'):
        checks['schema_change_only_in_wal'] = all(row.get('wal_only_schema') is True for row in rows)
    if mode in ('missing_wal', 'missing_shm'):
        checks['missing_sidecar_was_actually_injected'] = all(row.get('selected_sidecar_removed') is True for row in rows)
    if mode == 'timeout':
        from timeout_receipt import timeout_observation
        markers = [read_object(path) for path in (out / 'artifacts').rglob('timeout_ready.json')]
        observations = [read_object(path) for path in (out / 'artifacts').rglob('configured_observation.json')]
        checks['real_candidate_deadline_reached'] = (markers == [{'candidate_loop_checked': True}]
            and len(observations) == 1 and observations[0].get('error_kind') == 'ExtractionStopped')
        receipts = list((out / 'artifacts').rglob('completion.commit.json'))
        checks['committed_stage_timeout_proof'] = len(receipts) == 1 and len(observations) == 1 and (
            timeout_observation(receipts[0].parent, observations[0].get('run_id')) == {
                'same_configured_run': True, 'receipt_valid': True,
                'status': 'cleanup_unconfirmed', 'primary_status': 'deadline_exceeded',
                'deadline_kind': 'stage', 'timeout_stage': 'source_prepare'})
    return checks

"""Disabled-only MCP package entry. No configuration, network or source access."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parent
STAGE = 'runtime'


def check_runtime_versions(site, distributions):
    """Check only fixed pinned distributions; never enumerate installed packages."""
    for name, expected in distributions.items():
        if (name, expected) not in (('mcp','1.29.0'),('anyio','4.15.1'),('pydantic','2.13.5')):
            raise RuntimeError('Preview dependency contract is invalid')
        try:
            path=(site/(name+'-'+expected+'.dist-info')/'METADATA').resolve()
            if not path.is_relative_to(site.resolve()):
                raise ValueError()
            with path.open(encoding='utf-8') as stream:
                header=stream.read(4096).split('\n\n',1)
            if len(header)!=2:
                raise ValueError()
            fields={};previous=None
            for line in header[0].splitlines():
                if line[:1].isspace():
                    if previous in ('name','version'):raise ValueError()
                    continue
                key,separator,value=line.partition(':')
                if not separator:raise ValueError()
                previous=key.casefold()
                if previous in ('name','version'):
                    if previous in fields:raise ValueError()
                    fields[previous]=value.strip()
            if fields!={'name':name,'version':expected}:raise ValueError()
        except (OSError,UnicodeError,ValueError):
            raise RuntimeError('Preview dependency version does not match') from None


def build_server():
    global STAGE
    if len(sys.argv) != 1 or not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        raise RuntimeError('Preview requires its fixed isolated launch command')
    runtime = json.loads((ROOT / 'runtime_contract.json').read_text('utf-8'))
    if sys.version.split()[0] != runtime['python']:
        raise RuntimeError('Preview runtime version does not match')
    site = Path(runtime['site_packages']).resolve()
    if not site.is_dir() or site.name != 'site-packages':
        raise RuntimeError('Preview dependencies are unavailable')
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'source'))
    sys.path.extend((str(site), str(site / 'win32'), str(site / 'win32/lib')))
    check_runtime_versions(site,runtime['distributions'])
    STAGE = 'guard'
    from preview_guard import install
    guard = install(ROOT, site)
    STAGE = 'source_import'
    import line_mcp_server as server
    STAGE = 'tool_projection'
    for name in ('line_list_chats', 'line_get_history', 'line_get_unread', 'line_get_contacts'):
        server.mcp.remove_tool(name)
    # No trusted local source binding is permitted in this preview.
    def disabled_binding(*args, **kwargs):
        raise RuntimeError('Preview cannot bind any source')
    server.configure_schema = disabled_binding
    server._schema_service.configure = disabled_binding
    return server, guard


def main():
    global STAGE
    server, _guard = build_server()
    STAGE = 'stdio'
    server.mcp.run(transport='stdio')


def diagnostic(*args):
    sys.stderr.write('LINE disabled preview failed at ' + STAGE + '; details suppressed\n')


if __name__ == '__main__':
    sys.excepthook = diagnostic
    try:
        main()
    except BaseException:
        diagnostic()
        raise SystemExit(3) from None

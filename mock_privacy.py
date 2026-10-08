"""Synthetic choice negotiation only: never evidence of a human's approval.

Scope fingerprints and fresh preview generation IDs are non-secret, ephemeral
binding metadata, not credentials or live capabilities. Real data is absent.
"""
import hashlib
import json
import secrets
import time

DESTINATIONS = frozenset({'local-test','chatgpt','claude','gemini','grok','deepseek','qwen'})
CHOICES = frozenset({'deidentify','original'})
CONTROL_KEYS = frozenset({'destination','privacy_choice','mock_scope_id'})


class MockPrivacyChoice:
    def __init__(self, clock=time.monotonic, ttl_seconds=120):
        self.clock=clock
        self.ttl_seconds=ttl_seconds
        self._pending={}
        self._active={}

    def negotiate(self, tool, arguments):
        now=self.clock()
        self._pending={key:entry for key,entry in self._pending.items() if entry[1]>now}
        data={key:value for key,value in arguments.items() if key not in CONTROL_KEYS}
        destination=arguments.get('destination')
        known=isinstance(destination,str) and destination in DESTINATIONS
        choice=arguments.get('privacy_choice')
        selected=choice if isinstance(choice,str) and choice in CHOICES else None
        scope={'tool':tool,'destination':destination if known else None,
               'chat_id':data.get('chat_id'),'since':data.get('since'),'until':data.get('until'),
               'limit':data.get('limit'),'limit_chats':data.get('limit_chats'),
               'per_chat_limit':data.get('per_chat_limit'),'continuation':bool(data.get('cursor'))}
        query=data.get('query')
        if isinstance(query,str):
            scope['query_fingerprint']=hashlib.sha256(query.encode('utf-8')).hexdigest()
        body=json.dumps([tool,destination if known else None,data],sort_keys=True,ensure_ascii=True)
        fingerprint=hashlib.sha256(body.encode()).hexdigest() if known else None
        response={'destination':destination if known else None,'privacy_choice':selected,
                  'mock_scope_id':None,'scope_fingerprint':fingerprint,'request_scope':scope,
                  'human_confirmation_verified':False,'privacy_processing':'not_implemented'}
        generation=arguments.get('mock_scope_id')
        if known and selected and isinstance(generation,str):
            active=self._active.get(generation)
            # Expiry blocks new executions/joins; an already-started synthetic
            # read may finish, and still does not establish human authorization.
            if active and active[0]==fingerprint and active[1]==selected and active[3]>now:
                self._active[generation]=(active[0],active[1],active[2]+1,active[3])
                response['mock_scope_id']=generation
                return True,response,generation
            pending=self._pending.get(generation)
            if pending and pending[0]==fingerprint and pending[1]>now:
                self._pending.pop(generation)
                self._active[generation]=(fingerprint,selected,1,pending[1])
                response['mock_scope_id']=generation
                return True,response,generation
        if known:
            if len(self._pending)>=32:
                self._pending.pop(next(iter(self._pending)))
            # A refusal never resurrects an old generation ID. Replaying an
            # old chosen request repeatedly cannot manufacture a new choice.
            generation=secrets.token_hex(16)
            self._pending[generation]=(fingerprint,now+self.ttl_seconds)
            response['mock_scope_id']=generation
        response['error_code']=('destination_required' if not known else
                                'privacy_choice_required' if not selected else 'scope_confirmation_required')
        return False,response,None

    def release(self, generation):
        active=self._active.get(generation)
        if active is None:
            return
        if active[2]<=1:
            self._active.pop(generation,None)
        else:
            self._active[generation]=(active[0],active[1],active[2]-1,active[3])

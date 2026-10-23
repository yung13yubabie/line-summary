"""TEST ONLY exact CreateProcess validation before CPython 3.11's audit bug.

3.11.9 _winapi.c audits command_line (PyObject*) using the wide-string 'u'
format. Validate the actual Python call first; its audit must occur in that
same thread/window. No alternate creator or command is permitted.
"""
import threading

class NativeCreateGuard:
    def __init__(self, api, expected, inherited):
        self.original=api.CreateProcess
        self.expected=expected
        self.inherited=inherited
        self.local=threading.local()
        api.CreateProcess=self.create

    def create(self, *args):
        expected=self.expected()
        if (expected is None or len(args)!=9 or tuple(args[:2])!=expected
                or args[2] is not None or args[3] is not None
                or args[4] is not True or args[5]!=0x08000004
                or args[6] is not None or args[7] is not None):
            raise RuntimeError('only exact suspended synthetic native call allowed')
        startup=args[8]
        attributes=getattr(startup,'lpAttributeList',{})
        handles=attributes.get('handle_list') if type(attributes) is dict else None
        standard=tuple(getattr(startup,name,None) for name in ('hStdInput','hStdOutput','hStdError'))
        if (getattr(startup,'dwFlags',None)!=0x100 or type(handles) is not list
                or len(handles)!=4 or len(set(handles))!=4
                or any(type(value) is not int or value<=0 for value in handles)
                or standard!=tuple(handles[:3])
                or handles[-1]!=self.inherited()):
            raise RuntimeError('only exact explicit synthetic handle inheritance allowed')
        if getattr(self.local,'validated',None) is not None:
            raise RuntimeError('recursive native creation forbidden')
        self.local.validated=expected
        self.local.audited=False
        try:return self.original(*args)
        finally:
            self.local.validated=None
            self.local.audited=False

    def audit_matches(self, args, expected):
        # args[1] is deliberately not evidence on affected CPython versions.
        # The actual command string was compared before entering the C API.
        if (expected is None or len(args)<2 or args[0]!=expected[0]
                or getattr(self.local,'validated',None)!=expected
                or getattr(self.local,'audited',False)):
            return False
        self.local.audited=True
        return True

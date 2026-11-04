"""Shared decision errors, redirect policy and Windows local credentials."""
import ctypes
import os
import urllib.request


class GateError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def windows_secret(data, protect=False):
    """DPAPI uses this Windows login; Nano uses an environment variable instead."""
    if os.name != 'nt':
        raise GateError('Windows credential file requires the Windows login that created it')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = Blob()
    crypt = ctypes.windll.crypt32
    call = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    if not call(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise GateError('Cannot access the local Windows credential')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


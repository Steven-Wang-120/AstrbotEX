"""Nano-only half-duplex direction adapter with one ioctl per pin pair.

Uses the Linux GPIO v1 ABI already present in L4T 32.7.6. BCM17 maps to
tegra-gpio offset 50 and BCM27 to offset 14 in the installed NVIDIA Nano pin
table. It owns only those two lines. A grouped ioctl reduces userspace gaps;
it is not a guarantee of simultaneous electrical edges.
"""
import ctypes
import fcntl
import os
from pathlib import Path

class ChipInfo(ctypes.Structure):
    _fields_=[('name',ctypes.c_char*32),('label',ctypes.c_char*32),('lines',ctypes.c_uint32)]
class HandleRequest(ctypes.Structure):
    _fields_=[('offsets',ctypes.c_uint32*64),('flags',ctypes.c_uint32),
              ('defaults',ctypes.c_uint8*64),('consumer',ctypes.c_char*32),
              ('lines',ctypes.c_uint32),('fd',ctypes.c_int)]
class HandleData(ctypes.Structure):
    _fields_=[('values',ctypes.c_uint8*64)]

GET_CHIP=0x8044B401
GET_HANDLE=0xC16CB403
GET_VALUES=0xC040B408
SET_VALUES=0xC040B409

class GroupedDirectionGPIO:
    BCM='BCM';OUT='OUT';VERSION='scoped-two-line-v1'
    def __init__(self,profile,*,opener=None,closer=None,ioctl=None,compatible_reader=None):
        s=profile.raw['serial']
        if (s['rx_bcm'],s['tx_bcm'])!=(17,27) or s['rx_levels']!={'write':0,'read':1} or s['tx_levels']!={'write':1,'read':0}:
            raise ValueError('Grouped adapter requires the reviewed Nano BCM17/27 mapping')
        self._open=opener or os.open;self._close=closer or os.close;self._ioctl=ioctl or fcntl.ioctl
        self._compatible=compatible_reader or (lambda:Path('/proc/device-tree/compatible').read_bytes())
        self._mode=None;self._configured={};self._pending={};self._active={17:1,27:0}
        self._chip=None;self._handle=None;self.group_writes=0

    def setmode(self,mode):
        if mode!=self.BCM:raise ValueError('Only BCM mode is permitted')
        self._mode=mode
    def setwarnings(self,enabled):pass

    def setup(self,pin,direction,*,initial):
        if self._mode!=self.BCM or pin not in (17,27) or direction!=self.OUT:
            raise ValueError('Only two reviewed output direction lines are permitted')
        if initial!={17:1,27:0}[pin]:raise ValueError('Startup must be receive-only')
        if pin in self._configured:raise ValueError('GPIO already configured')
        self._configured[pin]=initial
        if len(self._configured)<2:return
        if b'nvidia,tegra210' not in self._compatible():raise ValueError('Only the verified Tegra210 Nano is supported')
        try:
            self._chip=self._open('/dev/gpiochip0',os.O_RDONLY|os.O_CLOEXEC)
            info=ChipInfo();self._ioctl(self._chip,GET_CHIP,info)
            if bytes(info.label).split(b'\0')[0]!=b'tegra-gpio' or info.lines<=50:
                raise ValueError('GPIO chip identity does not match Nano pin data')
            request=HandleRequest();request.offsets[0]=50;request.offsets[1]=14
            request.flags=2;request.defaults[0]=1;request.defaults[1]=0
            request.consumer=b'jetmax-readonly-direction';request.lines=2;request.fd=-1
            self._ioctl(self._chip,GET_HANDLE,request)
            if request.fd<0:raise OSError('No grouped GPIO handle returned')
            self._handle=request.fd
        except BaseException:
            self._release();raise

    def output(self,pin,value):
        if self._handle is None or pin not in (17,27) or type(value) is not int or value not in (0,1):
            raise ValueError('Unclaimed pin or invalid direction level')
        if pin in self._pending:
            self._pending.clear();raise ValueError('Direction transitions must provide a complete pair')
        self._pending[pin]=value
        if len(self._pending)<2:return
        pair=dict(self._pending);self._pending.clear()
        if (pair[17],pair[27]) not in ((1,0),(0,1)):
            raise ValueError('Only complete RX or TX states are permitted')
        data=HandleData();data.values[0]=pair[17];data.values[1]=pair[27]
        self._ioctl(self._handle,SET_VALUES,data)
        self._active=pair;self.group_writes+=1

    def input(self,pin):
        if self._handle is None or pin not in (17,27):raise ValueError('Unclaimed GPIO')
        data=HandleData();self._ioctl(self._handle,GET_VALUES,data)
        return int(data.values[0 if pin==17 else 1])

    def cleanup(self,channels):
        pins=[channels] if isinstance(channels,int) else list(channels)
        if not pins or any(p not in (17,27) for p in pins):raise ValueError('Only owned direction lines can be released')
        self._release();self._configured.clear();self._pending.clear()

    def _release(self):
        errors=[]
        for key in ('_handle','_chip'):
            fd=getattr(self,key)
            if fd is not None:
                try:self._close(fd)
                except OSError as exc:errors.append(str(exc))
                finally:setattr(self,key,None)
        if errors:raise OSError('; '.join(errors))

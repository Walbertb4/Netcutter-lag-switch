"""
network_control.py
Blocks/restores network access for one or more specific applications,
instead of cutting the whole system's internet connection - so things
like voice chat (Discord, Teams, etc.) and everything else on the PC
keep working while only the target app(s) are cut off.

Implementation: Windows Filtering Platform (WFP) directly via
fwpuclnt.dll (Base Filtering Engine / BFE) using a dynamic session.
Talking directly to BFE avoids both netsh latency and Windows Defender
Firewall policy-store errors (e.g. 0x80070002 / 0x2), applies and
removes per-app blocks in <1ms, and automatically cleans up all filters
if the process ever exits unexpectedly.
"""

import ctypes
import os
import threading
import uuid

_fwpuclnt = ctypes.WinDLL("fwpuclnt.dll")

RPC_C_AUTHN_WINNT = 10
FWPM_SESSION_FLAG_DYNAMIC = 0x00000001
FWP_EMPTY = 0
FWP_UINT8 = 1
FWP_BYTE_BLOB_TYPE = 12
FWP_MATCH_EQUAL = 0
FWP_ACTION_BLOCK = 0x00001001  # 0x1 | FWP_ACTION_FLAG_TERMINATING


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]

    @classmethod
    def from_str(cls, s: str):
        u = uuid.UUID(s)
        b = u.bytes_le
        return cls.from_buffer_copy(b)


class _FWPM_DISPLAY_DATA0(ctypes.Structure):
    _fields_ = [
        ("name", ctypes.c_wchar_p),
        ("description", ctypes.c_wchar_p),
    ]


class _FWP_BYTE_BLOB(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
    ]


class _FWP_VALUE0_UNION(ctypes.Union):
    _fields_ = [
        ("uint8", ctypes.c_uint8),
        ("uint64", ctypes.c_uint64),
        ("byteBlob", ctypes.POINTER(_FWP_BYTE_BLOB)),
    ]


class _FWP_VALUE0(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("u", _FWP_VALUE0_UNION),
    ]


class _FWPM_SESSION0(ctypes.Structure):
    _fields_ = [
        ("sessionKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("txnWaitTimeoutInMSec", ctypes.c_uint32),
        ("processId", ctypes.c_uint32),
        ("sid", ctypes.c_void_p),
        ("username", ctypes.c_wchar_p),
        ("kernelMode", ctypes.c_int),
    ]


class _FWPM_SUBLAYER0(ctypes.Structure):
    _fields_ = [
        ("subLayerKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.c_void_p),
        ("providerData", _FWP_BYTE_BLOB),
        ("weight", ctypes.c_uint16),
    ]


class _FWPM_FILTER_CONDITION0(ctypes.Structure):
    _fields_ = [
        ("fieldKey", _GUID),
        ("matchType", ctypes.c_uint32),
        ("conditionValue", _FWP_VALUE0),
    ]


class _FWPM_ACTION0(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("filterType", _GUID),
    ]


class _FWPM_CONTEXT_UNION(ctypes.Union):
    _fields_ = [
        ("rawContext", ctypes.c_uint64),
        ("providerContextKey", _GUID),
    ]


class _FWPM_FILTER0(ctypes.Structure):
    _fields_ = [
        ("filterKey", _GUID),
        ("displayData", _FWPM_DISPLAY_DATA0),
        ("flags", ctypes.c_uint32),
        ("providerKey", ctypes.c_void_p),
        ("providerData", _FWP_BYTE_BLOB),
        ("layerKey", _GUID),
        ("subLayerKey", _GUID),
        ("weight", _FWP_VALUE0),
        ("numFilterConditions", ctypes.c_uint32),
        ("filterCondition", ctypes.POINTER(_FWPM_FILTER_CONDITION0)),
        ("action", _FWPM_ACTION0),
        ("context", _FWPM_CONTEXT_UNION),
        ("reserved", ctypes.c_void_p),
        ("filterId", ctypes.c_uint64),
        ("effectiveWeight", _FWP_VALUE0),
    ]


_fwpuclnt.FwpmEngineOpen0.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.POINTER(_FWPM_SESSION0),
    ctypes.POINTER(ctypes.c_void_p),
]
_fwpuclnt.FwpmEngineOpen0.restype = ctypes.c_uint32

_fwpuclnt.FwpmEngineClose0.argtypes = [ctypes.c_void_p]
_fwpuclnt.FwpmEngineClose0.restype = ctypes.c_uint32

_fwpuclnt.FwpmSubLayerAdd0.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_FWPM_SUBLAYER0),
    ctypes.c_void_p,
]
_fwpuclnt.FwpmSubLayerAdd0.restype = ctypes.c_uint32

_fwpuclnt.FwpmGetAppIdFromFileName0.argtypes = [
    ctypes.c_wchar_p,
    ctypes.POINTER(ctypes.POINTER(_FWP_BYTE_BLOB)),
]
_fwpuclnt.FwpmGetAppIdFromFileName0.restype = ctypes.c_uint32

_fwpuclnt.FwpmFreeMemory0.argtypes = [ctypes.c_void_p]
_fwpuclnt.FwpmFreeMemory0.restype = None

_fwpuclnt.FwpmFilterAdd0.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(_FWPM_FILTER0),
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint64),
]
_fwpuclnt.FwpmFilterAdd0.restype = ctypes.c_uint32

_fwpuclnt.FwpmFilterDeleteById0.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
_fwpuclnt.FwpmFilterDeleteById0.restype = ctypes.c_uint32

_LAYERS = (
    _GUID.from_str("c38d57d1-05a7-4c33-904f-7fbceee60e82"),  # ALE_AUTH_CONNECT_V4
    _GUID.from_str("4a72393b-319f-44bc-84c3-ba54dcb3b6b4"),  # ALE_AUTH_CONNECT_V6
    _GUID.from_str("e1cd9fe7-f4b5-4273-96c0-592e487b8650"),  # ALE_AUTH_RECV_ACCEPT_V4
    _GUID.from_str("a3b42c97-9f04-4672-b87e-cee9c483257f"),  # ALE_AUTH_RECV_ACCEPT_V6
)
_COND_ALE_APP_ID = _GUID.from_str("d78e1e87-8644-4ea5-9437-d809ecefc971")
_SUBLAYER_GUID = _GUID.from_str("9b4e3c21-6f18-4d0a-a2e5-7c1b8e904d31")

_lock = threading.Lock()
_engine = ctypes.c_void_p()
_active_filter_ids = []


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _ensure_engine():
    global _engine
    if _engine.value:
        return _engine

    session = _FWPM_SESSION0()
    session.displayData.name = "NetCutter Dynamic Session"
    session.flags = FWPM_SESSION_FLAG_DYNAMIC

    handle = ctypes.c_void_p()
    rc = _fwpuclnt.FwpmEngineOpen0(
        None, RPC_C_AUTHN_WINNT, None, ctypes.byref(session), ctypes.byref(handle)
    )
    if rc != 0:
        raise RuntimeError(f"WFP FwpmEngineOpen0 failed (0x{rc:08X})")

    # Dedicated high-weight sublayer ensures Windows Firewall Allow rules
    # in other sublayers cannot override our block filters.
    sublayer = _FWPM_SUBLAYER0()
    sublayer.subLayerKey = _SUBLAYER_GUID
    sublayer.displayData.name = "NetCutter Block Sublayer"
    sublayer.weight = 0xFFFF
    rc_sl = _fwpuclnt.FwpmSubLayerAdd0(handle, ctypes.byref(sublayer), None)
    # 0x80320009 = FWP_E_ALREADY_EXISTS
    if rc_sl not in (0, 0x80320009):
        _fwpuclnt.FwpmEngineClose0(handle)
        raise RuntimeError(f"WFP FwpmSubLayerAdd0 failed (0x{rc_sl:08X})")

    _engine = handle
    return _engine


def cut_apps(program_paths):
    """Blocks all inbound and outbound traffic for each given .exe via WFP."""
    with _lock:
        engine = _ensure_engine()
        for path in program_paths:
            norm_path = os.path.normpath(os.path.abspath(path))
            blob_ptr = ctypes.POINTER(_FWP_BYTE_BLOB)()
            rc = _fwpuclnt.FwpmGetAppIdFromFileName0(norm_path, ctypes.byref(blob_ptr))
            if rc != 0:
                raise RuntimeError(
                    f"WFP FwpmGetAppIdFromFileName0 failed for {norm_path} (0x{rc:08X})"
                )
            try:
                cond = _FWPM_FILTER_CONDITION0()
                cond.fieldKey = _COND_ALE_APP_ID
                cond.matchType = FWP_MATCH_EQUAL
                cond.conditionValue.type = FWP_BYTE_BLOB_TYPE
                cond.conditionValue.u.byteBlob = blob_ptr

                for layer in _LAYERS:
                    flt = _FWPM_FILTER0()
                    flt.displayData.name = "NetCutter App Block"
                    flt.layerKey = layer
                    flt.subLayerKey = _SUBLAYER_GUID
                    flt.weight.type = FWP_UINT8
                    flt.weight.u.uint8 = 15
                    flt.numFilterConditions = 1
                    flt.filterCondition = ctypes.pointer(cond)
                    flt.action.type = FWP_ACTION_BLOCK

                    fid = ctypes.c_uint64(0)
                    rc_add = _fwpuclnt.FwpmFilterAdd0(
                        engine, ctypes.byref(flt), None, ctypes.byref(fid)
                    )
                    if rc_add != 0:
                        raise RuntimeError(f"WFP FwpmFilterAdd0 failed (0x{rc_add:08X})")
                    _active_filter_ids.append(fid.value)
            finally:
                _fwpuclnt.FwpmFreeMemory0(ctypes.byref(blob_ptr))


def restore_apps():
    """Removes the active WFP block filters."""
    with _lock:
        if not _engine.value or not _active_filter_ids:
            _active_filter_ids.clear()
            return
        for fid in _active_filter_ids:
            try:
                _fwpuclnt.FwpmFilterDeleteById0(_engine, fid)
            except Exception:
                pass
        _active_filter_ids.clear()


def force_clean():
    """Removes any active filters and closes the dynamic WFP session."""
    global _engine
    restore_apps()
    with _lock:
        if _engine.value:
            try:
                _fwpuclnt.FwpmEngineClose0(_engine)
            except Exception:
                pass
            _engine = ctypes.c_void_p()

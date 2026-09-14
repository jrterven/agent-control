"""macOS Keychain through Security.framework; secrets never enter process argv."""
from __future__ import annotations
import ctypes


class MacKeychain:
    def __init__(self):
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        self.security.SecKeychainFindGenericPassword.argtypes = [pointer, uint, pointer, uint, pointer,
                                                               ctypes.POINTER(uint), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        self.security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainAddGenericPassword.argtypes = [pointer, uint, pointer, uint, pointer, uint, pointer, ctypes.POINTER(pointer)]
        self.security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
        self.security.SecKeychainItemModifyAttributesAndData.argtypes = [pointer, pointer, uint, pointer]
        self.security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
        self.security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
        self.security.SecKeychainItemDelete.argtypes = [pointer]
        self.security.SecKeychainItemDelete.restype = ctypes.c_int32
        self.foundation.CFRelease.argtypes = [pointer]

    def _find(self, service: str, account: str):
        service_bytes, account_bytes = service.encode(), account.encode()
        length, data, item = ctypes.c_uint32(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.security.SecKeychainFindGenericPassword(None, len(service_bytes), service_bytes,
            len(account_bytes), account_bytes, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))
        value = None
        if status == 0:
            try:
                if length.value > 64 * 1024:
                    raise RuntimeError("Invalid connector Keychain item")
                value = ctypes.string_at(data, length.value)
            finally:
                self.security.SecKeychainItemFreeContent(None, data)
        elif status != -25300:  # errSecItemNotFound
            raise RuntimeError("Unable to access connector credentials in Keychain")
        return value, item

    def load(self, service: str, account: str) -> bytes:
        value, item = self._find(service, account)
        try:
            if value is None:
                raise RuntimeError("Connector credentials are unavailable in Keychain")
            return value
        finally:
            if item:
                self.foundation.CFRelease(item)

    def save(self, service: str, account: str, value: bytes):
        if len(value) > 64 * 1024:
            raise ValueError("Connector credentials exceed Keychain size limit")
        _, item = self._find(service, account)
        try:
            if item:
                status = self.security.SecKeychainItemModifyAttributesAndData(item, None, len(value), value)
            else:
                service_bytes, account_bytes = service.encode(), account.encode()
                status = self.security.SecKeychainAddGenericPassword(None, len(service_bytes), service_bytes,
                    len(account_bytes), account_bytes, len(value), value, None)
            if status != 0:
                raise RuntimeError("Unable to save connector credentials to Keychain")
        finally:
            if item:
                self.foundation.CFRelease(item)

    def delete(self, service: str, account: str):
        _, item = self._find(service, account)
        try:
            if item and self.security.SecKeychainItemDelete(item) != 0:
                raise RuntimeError("Unable to remove connector credentials from Keychain")
        finally:
            if item:
                self.foundation.CFRelease(item)

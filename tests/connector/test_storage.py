import ctypes
import json
from unittest.mock import MagicMock

import pytest
from agent_control_connector.storage import SecretStore, read_json
from agent_control_connector.keychain import MacKeychain


def test_macos_keychain_save_load_delete_use_native_calls_without_subprocess(tmp_path, monkeypatch):
    import agent_control_connector.storage as storage
    import agent_control_connector.keychain as keychain
    native = MagicMock()
    native.load.return_value = b'{"accessToken":"test-placeholder"}'
    monkeypatch.setattr(storage.sys,"platform","darwin")
    monkeypatch.setattr(keychain,"MacKeychain",lambda:native)
    monkeypatch.setattr(storage.subprocess,"run",lambda *a,**k:pytest.fail("Credentials must not enter a subprocess"))
    store = SecretStore(tmp_path)
    store.save({"accessToken":"test-placeholder"})
    assert store.load() == {"accessToken":"test-placeholder"}
    store.delete()
    native.save.assert_called_once()
    native.load.assert_called_once()
    native.delete.assert_called_once()
    assert not (tmp_path/"secrets.json").exists()


def test_linux_credential_files_are_private_and_removed(tmp_path,monkeypatch):
    import agent_control_connector.storage as storage
    monkeypatch.setattr(storage.sys,"platform","linux")
    store=SecretStore(tmp_path)
    store.save({"accessToken":"test-placeholder"})
    assert (tmp_path/"secrets.json").stat().st_mode & 0o777 == 0o600
    assert store.load()["accessToken"] == "test-placeholder"
    (tmp_path/"secrets.json").chmod(0o644)
    with pytest.raises(ValueError):
        store.load()
    store.delete()
    assert not (tmp_path/"secrets.json").exists()


def test_native_keychain_api_handles_missing_create_update_read_and_delete(monkeypatch):
    security,foundation=MagicMock(),MagicMock()
    monkeypatch.setattr(ctypes,"CDLL",lambda path:security if "Security.framework" in path else foundation)
    stored={}
    buffers=[]
    def find(_,service_length,service,account_length,account,length,data,item):
        if (service,account) not in stored:
            return -25300
        value=stored[(service,account)]
        buffer=ctypes.create_string_buffer(value)
        buffers.append(buffer)
        ctypes.cast(length,ctypes.POINTER(ctypes.c_uint32))[0]=len(value)
        ctypes.cast(data,ctypes.POINTER(ctypes.c_void_p))[0]=ctypes.cast(buffer,ctypes.c_void_p)
        ctypes.cast(item,ctypes.POINTER(ctypes.c_void_p))[0]=ctypes.c_void_p(1)
        return 0
    def add(_,service_length,service,account_length,account,length,value,item):
        stored[(service,account)]=value
        return 0
    def modify(item,attributes,length,value):
        stored[(b"service",b"account")]=value
        return 0
    security.SecKeychainFindGenericPassword.side_effect=find
    security.SecKeychainAddGenericPassword.side_effect=add
    security.SecKeychainItemModifyAttributesAndData.side_effect=modify
    security.SecKeychainItemDelete.return_value=0
    native=MacKeychain()
    with pytest.raises(RuntimeError,match="unavailable"):
        native.load("service","account")
    native.save("service","account",b"first-test-placeholder")
    assert native.load("service","account") == b"first-test-placeholder"
    native.save("service","account",b"second-test-placeholder")
    assert native.load("service","account") == b"second-test-placeholder"
    native.delete("service","account")
    security.SecKeychainAddGenericPassword.assert_called_once()
    security.SecKeychainItemModifyAttributesAndData.assert_called_once()
    security.SecKeychainItemDelete.assert_called_once()


def test_ledger_receipt_budget_preserves_all_dedupe_tombstones(tmp_path,monkeypatch):
    from agent_control_connector.storage import OperationLedger
    monkeypatch.setattr(OperationLedger,"MAX_RECEIPT_BYTES",16)
    monkeypatch.setattr(OperationLedger,"MAX_RECEIPTS_BYTES",20)
    ledger=OperationLedger(tmp_path)
    assert ledger.reserve("one","hash-one")[0] == "new"
    ledger.finish("one",b"1"*12)
    assert ledger.reserve("two","hash-two")[0] == "new"
    ledger.finish("two",b"2"*12)
    assert ledger.receipt_bytes == 12
    assert ledger.reserve("two","hash-two")[0] == "unknown"
    ledger.close()
    reopened=OperationLedger(tmp_path)
    assert reopened.receipt_bytes == 12
    assert reopened.reserve("one","hash-one") == ("completed",b"1"*12)
    assert reopened.reserve("two","hash-two") == ("unknown",None)
    page_size=reopened.db.execute("PRAGMA page_size").fetchone()[0]
    assert reopened.db.execute("PRAGMA max_page_count").fetchone()[0]*page_size <= OperationLedger.MAX_DATABASE_BYTES
    assert (tmp_path/"operations.sqlite3").stat().st_mode & 0o777 == 0o600
    reopened.close()


def test_ledger_count_limit_never_removes_old_operation_ids(tmp_path,monkeypatch):
    from agent_control_connector.storage import OperationLedger
    monkeypatch.setattr(OperationLedger,"MAX_OPERATIONS",1)
    ledger=OperationLedger(tmp_path)
    assert ledger.reserve("one","hash")[0] == "new"
    assert ledger.reserve("two","hash") == ("full",None)
    assert ledger.reserve("one","hash")[0] == "running"
    ledger.close()

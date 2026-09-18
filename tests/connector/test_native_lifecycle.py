"""Native-runner smoke checks without API dependencies or real service startup.

Set AGENT_CONTROL_CONNECTOR_ARCHIVE to exercise the frozen release instead of
source. The lifecycle worker below is an isolated subprocess in pytest's temp
folder; launchctl/systemctl registration and Hermes are never started.
"""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time

import pytest
from agent_control_connector import manage
from agent_control_connector.storage import SecretStore

REPO=Path(__file__).resolve().parents[2]


@pytest.fixture
def native_command(tmp_path):
    archive=os.environ.get("AGENT_CONTROL_CONNECTOR_ARCHIVE")
    environment=os.environ.copy()
    environment.pop("PYTHONHOME",None)
    if archive:
        with tarfile.open(Path(archive).resolve()) as source:
            for member in source.getmembers():
                path=Path(member.name)
                assert not path.is_absolute() and ".." not in path.parts
                assert member.isfile() or member.isdir()
            source.extractall(tmp_path/"native",filter="data")
        environment.pop("PYTHONPATH",None)
        return [str(tmp_path/"native/agent-control-connector/agent-control-connector")],environment
    environment["PYTHONPATH"]=os.pathsep.join(str(REPO/path) for path in ("packages/connector","packages/hermes-client"))
    return [sys.executable,"-m","agent_control_connector"],environment


def test_native_cli_version_status_and_failure_exit_codes(native_command,tmp_path):
    command,environment=native_command
    def invoke(*args):
        return subprocess.run([*command,*args],env=environment,cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert invoke("--version").stdout.strip() == "0.1.0"
    media = invoke("check-media")
    assert media.returncode == 0 and media.stdout.strip() == "ok", media.stderr
    status=invoke("status","--data-dir",str(tmp_path/"unpaired"))
    assert status.returncode == 0
    assert json.loads(status.stdout)["activeWork"] is None
    doctor=invoke("doctor","--data-dir",str(tmp_path/"unpaired"))
    assert doctor.returncode == 0 and json.loads(doctor.stdout)["paired"] is False
    assert not (tmp_path/"unpaired").exists()
    assert invoke("check-credentials", "--data-dir", str(tmp_path/"unpaired")).returncode == 1
    assert not (tmp_path/"unpaired").exists()
    assert invoke("invalid-command").returncode == 2
    reconnect_help = invoke("install-service", "--help")
    assert reconnect_help.returncode == 0 and "--reconnect" in reconnect_help.stdout
    assert invoke("run","--data-dir",str(tmp_path/"unpaired")).returncode == 1


def release(path,version):
    path.mkdir(parents=True)
    binary=path/"agent-control-connector"
    binary.write_text(f'''#!{sys.executable}
import json,os,sys
from pathlib import Path
if sys.argv[1] == "connect":
    home=Path(sys.argv[sys.argv.index("--data-dir")+1])
    target=home/"config.json"
    target.write_text(json.dumps({{"server":sys.argv[sys.argv.index("--server")+1]}}))
    target.chmod(0o600)
elif sys.argv[1] == "--help":
    print("{{connect,check-credentials,run,status}}")
elif sys.argv[1] == "check-credentials":
    print("Existing connector credentials are accessible.")
''')
    binary.chmod(0o755)
    (path/"release.json").write_text(json.dumps({"revision":version,"protocol":1,"system":platform.system(),"architecture":platform.machine()}))
    return path


def test_generated_native_service_file_is_valid_without_loading_it(tmp_path,monkeypatch):
    fake_user=tmp_path/"user"
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:fake_user))
    home=fake_user/"connector"
    bundle=release(home/"releases/r1","r1")
    (home/"current").symlink_to(bundle)
    manage.write_service(home)
    unit=manage.service_path()
    assert unit.stat().st_mode & 0o777 == 0o600
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/plutil","-lint",str(unit)],check=True,capture_output=True)
    elif shutil.which("systemd-analyze"):
        result=subprocess.run(["systemd-analyze","verify","--man=no",str(unit)],capture_output=True,text=True)
        assert result.returncode == 0,result.stderr
    else:
        pytest.skip("Native systemd unit verifier is not installed")


def test_native_process_install_update_rollback_remove_and_repair(tmp_path,monkeypatch):
    fake_user=tmp_path/"user"
    monkeypatch.setattr(Path,"home",classmethod(lambda cls:fake_user))
    home=fake_user/"connector"
    first=release(tmp_path/"r1","r1")
    second=release(tmp_path/"r2","r2")
    worker=tmp_path/"worker.py"
    worker.write_text('''import json,signal,sys,time
from datetime import datetime,timezone
from pathlib import Path
home=Path(sys.argv[1]); running=True
def stop(*args):
    global running
    running=False
signal.signal(signal.SIGTERM,stop)
while running:
    marker=home/"maintenance.request"
    try: identity=marker.read_text()
    except FileNotFoundError: identity=None
    value={"activeWork":False,"fresh":True,"connected":True,"observedAt":datetime.now(timezone.utc).isoformat(),"maintenance":identity is not None,"maintenanceRequestId":identity}
    temporary=home/"status.worker.tmp"
    temporary.write_text(json.dumps(value)); temporary.replace(home/"status.json")
    time.sleep(.02)
''')
    processes=[]
    actions=[]
    def service(action):
        actions.append((action,(home/"current").resolve().name))
        if action == "start":
            processes.append(subprocess.Popen([sys.executable,str(worker),str(home)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
        elif processes and processes[-1].poll() is None:
            processes[-1].terminate(); processes[-1].wait(timeout=5)
    # Stand-in worker owns runtime.lock exactly as the real runtime does.
    text=worker.read_text().replace('home=Path(sys.argv[1]); running=True','import fcntl\nhome=Path(sys.argv[1]); running=True\nlock=(home/"runtime.lock").open("w"); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)')
    worker.write_text(text)
    monkeypatch.setattr(manage,"service",service)
    os_commands=[]
    monkeypatch.setattr(manage,"run",lambda *args,**kwargs:os_commands.append(args))
    deleted=[]
    monkeypatch.setattr(SecretStore,"delete",lambda self:deleted.append(self.directory))
    def downloaded(root,stage,version):
        target=stage/"release"
        shutil.copytree(second,target)
        return target
    monkeypatch.setattr(manage,"verified_release",downloaded)
    def command(*args):
        return manage.management_main([*args,"--data-dir",str(home)])
    def ready():
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            try:
                if manage.status(home)["connected"] and manage.runtime_running(home):
                    return
            except (ValueError,FileNotFoundError):
                pass
            time.sleep(.02)
        pytest.fail("Isolated lifecycle worker failed to start")
    try:
        assert command("install-service","--source",str(first),"--release","r1","--server","https://control.test") == 0
        ready()
        ledger=home/"operations.sqlite3"
        ledger.write_bytes(b"deduplication-tombstones-placeholder")
        assert command("update","--release","r2") == 0
        assert (home/"current").resolve().name == "r2"
        assert command("rollback") == 0
        assert (home/"current").resolve().name == "r1"
        assert command("uninstall") == 0
        assert (home/"config.json").is_file() and not manage.service_path().exists()
        assert command("install-service") == 0
        ready()
        assert command("uninstall","--forget") == 0
        assert deleted == [home] and not (home/"config.json").exists()
        assert ledger.read_bytes() == b"deduplication-tombstones-placeholder"
        assert command("install-service","--source",str(first),"--release","r1","--server","https://control.test") == 0
        ready()
        assert (home/"config.json").is_file()
        assert all("hermes" not in " ".join(args).lower() for args in os_commands)
        assert actions[:6] == [("start","r1"),("stop","r1"),("start","r2"),("stop","r2"),("start","r1"),("stop","r1")]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=5)

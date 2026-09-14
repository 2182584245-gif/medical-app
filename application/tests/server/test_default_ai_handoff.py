"""Synthetic-only key delivery; no real default key, SSH host or cloud API."""

import base64
import json
import os
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ollama_chat_app.security import private_payload
from ollama_chat_app.security.secret_store import SecretStore
from tools.aliyun_platform import handoff_default_ai as sender
from tools.aliyun_platform import receive_default_ai as receiver
from tools.aliyun_platform.guard import DeploymentError

DEPLOYMENT = "a" * 32
SYNTHETIC_KEY = "sk-synthetic-default-ai-key-never-real"


def packet():
    key, nonce = bytes(range(32)), bytes(range(12))
    envelope = (
        sender.MAGIC
        + nonce
        + AESGCM(key).encrypt(nonce, SYNTHETIC_KEY.encode(), b"HealthLife/server-default-ai/v1")
    )
    return {
        "version": 1,
        "deployment_id": DEPLOYMENT,
        "kek": base64.b64encode(key).decode(),
        "envelope": base64.b64encode(envelope).decode(),
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI handoff only")
def test_prepare_creates_only_dpapi_private_package_and_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(SecretStore, "get_default_api_key", lambda *_: SYNTHETIC_KEY)
    package = tmp_path / "private.dpapi"
    assert sender.prepare(package, DEPLOYMENT)["plaintext_key_saved"] is False
    ciphertext = package.read_bytes()
    assert SYNTHETIC_KEY.encode() not in ciphertext and b'"kek"' not in ciphertext
    value = private_payload.read_private_json(package, "default-ai-handoff/" + DEPLOYMENT)
    assert json.loads(sender._validated_payload(value, DEPLOYMENT)) == value
    with pytest.raises(DeploymentError, match="not replaced"):
        sender.prepare(package, DEPLOYMENT)
    assert package.read_bytes() == ciphertext


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI handoff only")
def test_prepare_refuses_source_tree_before_key_read(monkeypatch):
    monkeypatch.setattr(SecretStore, "get_default_api_key", lambda *_: pytest.fail("No real Key"))
    with pytest.raises(DeploymentError, match="outside the source"):
        sender.prepare(sender.SOURCE / "must-not-exist.dpapi", DEPLOYMENT)


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI handoff only")
def test_prepare_racing_creator_is_never_replaced(tmp_path, monkeypatch):
    package = tmp_path / "race.dpapi"
    monkeypatch.setattr(SecretStore, "get_default_api_key", lambda *_: SYNTHETIC_KEY)
    original = private_payload.protect_payload

    def racing(data, purpose):
        package.write_bytes(b"synthetic-earlier-writer")
        return original(data, purpose)

    monkeypatch.setattr(private_payload, "protect_payload", racing)
    with pytest.raises(FileExistsError):
        sender.prepare(package, DEPLOYMENT)
    assert package.read_bytes() == b"synthetic-earlier-writer"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI handoff only")
def test_send_uses_pinned_ssh_stdin_and_sanitizes_untrusted_receipt(tmp_path, monkeypatch):
    package = tmp_path / "private.dpapi"
    private_payload.write_private_json(package, "default-ai-handoff/" + DEPLOYMENT, packet())
    key, known, ssh = [tmp_path / name for name in ("key", "known", "ssh")]
    for path in (key, known, ssh):
        path.write_text("synthetic-not-a-real-credential")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert json.loads(kwargs["input"]) == packet()
        assert SYNTHETIC_KEY.encode() not in kwargs["input"]
        assert packet()["kek"] not in " ".join(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "default_ai_installed",
                    "deployment_id": DEPLOYMENT,
                    "unexpected": SYNTHETIC_KEY,
                }
            ).encode(),
        )

    monkeypatch.setattr(sender.subprocess, "run", run)
    result = sender.handoff(
        package=package,
        host="synthetic.example",
        deployment_id=DEPLOYMENT,
        key=key,
        known_hosts=known,
        ssh=ssh,
        confirm=True,
    )
    assert SYNTHETIC_KEY not in json.dumps(result) and len(calls) == 1
    assert all(
        option in calls[0]
        for option in (
            "StrictHostKeyChecking=yes",
            "IdentitiesOnly=yes",
            "BatchMode=yes",
            "ProxyCommand=none",
            "ClearAllForwardings=yes",
            "ForwardAgent=no",
        )
    )


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"deployment_id": "b" * 32},
        {"kek": "broken"},
        {"plaintext_key": SYNTHETIC_KEY},
        {"envelope": "bad"},
    ],
)
def test_malformed_packet_refused_before_sender_or_receiver_io(change, monkeypatch):
    value = {**packet(), **change}
    with pytest.raises(DeploymentError, match="invalid"):
        sender._validated_payload(value, DEPLOYMENT)
    monkeypatch.setattr(receiver, "os", SimpleNamespace(name="posix", geteuid=lambda: 0))
    monkeypatch.setattr(receiver, "host_root", lambda: None)
    monkeypatch.setattr(receiver, "marker", lambda _path: {"id": DEPLOYMENT})
    monkeypatch.setattr(receiver, "_private_directory", lambda _path: pytest.fail("No file write"))
    with pytest.raises(DeploymentError):
        receiver.receive(json.dumps(value).encode(), DEPLOYMENT)


def test_actual_linux_receiver_private_permissions_replay_and_runtime_decryption():
    if os.environ.get("HEALTHLIFE_RUN_AI_CONTAINER_TESTS") != "synthetic-local-only":
        pytest.skip("explicit local synthetic Linux receiver gate required")
    nonce = uuid.uuid4().hex
    label, name = "healthlife.synthetic-ai-receiver", "healthlife-ai-receiver-" + nonce
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DOCKER_", "BUILDX_", "BUILDKIT_"))
    }

    def docker(*args, data=None, check=True):
        return subprocess.run(
            ["docker", "--context", "desktop-linux", *args],
            input=data,
            capture_output=True,
            timeout=45,
            check=check,
            env=environment,
        )

    context = json.loads(docker("context", "inspect", "desktop-linux").stdout)
    assert context[0]["Endpoints"]["docker"]["Host"] == "npipe:////./pipe/dockerDesktopLinuxEngine"
    image = (
        docker("image", "inspect", "medical-app-aliyun-api:v3", "--format", "{{.Id}}")
        .stdout.decode()
        .strip()
    )
    script = """
import base64,json,os,stat,sys
from pathlib import Path
from tools.aliyun_platform.receive_default_ai import receive
from tools.aliyun_platform.guard import DeploymentError
from server.platform_ai_proxy import load_encrypted_key
value=json.loads(sys.stdin.buffer.read())
root=Path('/opt/medical-app'); (root/'secrets').mkdir()
(root/'deployment.json').write_text(json.dumps({'kind':'medical-app:aliyun:v1','id':'a'*32,'public_host':'synthetic.example'}))
raw=json.dumps(value).encode()
assert receive(raw,'a'*32)['status']=='default_ai_installed'
assert receive(raw,'a'*32)['status']=='default_ai_already_installed'
key=root/'secrets/ai-key/default-ai-kek.bin'
envelope=root/'secrets/ai-envelope/deepseek-key.aesgcm'
before=(key.read_bytes(),envelope.read_bytes())
assert all(p.stat().st_uid==10001 and stat.S_IMODE(p.stat().st_mode)==0o400 for p in (key,envelope))
bad=dict(value); bad['kek']=base64.b64encode(bytes(reversed(range(32)))).decode()
try: receive(json.dumps(bad).encode(),'a'*32)
except DeploymentError: pass
else: raise AssertionError('different material accepted')
assert before==(key.read_bytes(),envelope.read_bytes())
os.setegid(10001); os.seteuid(10001)
assert load_encrypted_key(str(envelope),str(key))=='sk-synthetic-default-ai-key-never-real'
os.seteuid(0); os.setegid(0)
os.chmod(key,0o644)
try: receive(raw,'a'*32)
except DeploymentError: pass
else: raise AssertionError('unsafe permissions accepted')
os.chmod(key,0o400)
link=key.with_name('extra-link'); os.link(key,link)
try: receive(raw,'a'*32)
except DeploymentError: pass
else: raise AssertionError('hard link accepted')
assert before==(key.read_bytes(),envelope.read_bytes())
link.unlink(); saved=key.with_name('original'); key.rename(saved); key.symlink_to(saved)
try: receive(raw,'a'*32)
except DeploymentError: pass
else: raise AssertionError('symbolic link accepted')
print(json.dumps({'synthetic_only':True,'permissions':True,'same_retry':True,'different_refused':True,'runtime_decrypt':True,'unsafe_links_refused':True}))
"""
    command = [
        "run",
        "--name",
        name,
        "--label",
        label + "=" + nonce,
        "--pull=never",
        "--network=none",
        "--log-driver=none",
        "--memory=192m",
        "--pids-limit=64",
        "--read-only",
        "--user=0:0",
        "--cap-drop=ALL",
        "--cap-add=CHOWN",
        "--cap-add=DAC_OVERRIDE",
        "--cap-add=FOWNER",
        "--cap-add=SETUID",
        "--cap-add=SETGID",
        "--security-opt=no-new-privileges:true",
        "--tmpfs",
        "/opt/medical-app:rw,noexec,nosuid,size=4m",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=8m",
    ]
    for module in ("receive_default_ai.py", "guard.py"):
        source = sender.SOURCE / "tools/aliyun_platform" / module
        command.extend(
            [
                "--mount",
                f"type=bind,source={source},target=/app/tools/aliyun_platform/{module},readonly",
            ]
        )
    command.extend(["--entrypoint=python", "-i", image, "-c", script])
    try:
        result = docker(*command, data=json.dumps(packet()).encode(), check=False)
        assert result.returncode == 0, result.stderr.decode()[-2000:]
        assert json.loads(result.stdout)["runtime_decrypt"] is True
    finally:
        owned = docker("inspect", name, check=False)
        if owned.returncode == 0:
            info = json.loads(owned.stdout)[0]
            assert info["Config"]["Labels"].get(label) == nonce
            docker("rm", "--force", info["Id"])

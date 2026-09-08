"""Local-only project-token configuration and explicitly requested read-only checks.

This administrator helper never deploys, changes variables, reads user records,
or accepts a token/endpoint on the command line. Configure and status do not
connect to Railway. Returned secret payloads are for trusted in-process use
only: never print, log, serialize to plaintext, or include them in exceptions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .cloud_connection import hidden_input
from .local_secret_store import load_secret_payload, save_secret_payload

DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / ".local" / "railway-project-token.json"
TARGET_PROJECT_ID = "d27b4714-c7af-4cfa-bf8d-4f6fd61969d6"
TARGET_ENVIRONMENT_ID = "574e5ca1-a0c2-4ce8-868a-4cd165f9fe9a"
TARGET_SERVICE_ID = "afa6011e-0269-4fff-9b18-9a7b57f00093"
PURPOSE = "railway-project-deployment-v1"
API_URL = "https://backboard.railway.com/graphql/v2"
IDENTITY_QUERY = "query { projectToken { projectId environmentId } }"
MAX_RESPONSE_BYTES = 64 * 1024
PROFILE_KEYS = frozenset({"purpose", "project_id", "environment_id", "service_id", "token"})


class RailwayConfigurationError(ValueError):
    """Fixed, credential-free messages safe to show to the local operator."""


def _public_report(status_value: str, **fields) -> dict:
    return {
        "status": status_value,
        "project_id": TARGET_PROJECT_ID,
        "environment_id": TARGET_ENVIRONMENT_ID,
        "service_id": TARGET_SERVICE_ID,
        **fields,
    }


def _validated_payload(payload: dict) -> dict:
    if (
        type(payload) is not dict
        or set(payload) != PROFILE_KEYS
        or payload["purpose"] != PURPOSE
        or payload["project_id"] != TARGET_PROJECT_ID
        or payload["environment_id"] != TARGET_ENVIRONMENT_ID
        or payload["service_id"] != TARGET_SERVICE_ID
    ):
        raise RailwayConfigurationError("加密配置的项目、环境、服务或用途不匹配，已停止。")
    token = payload["token"]
    if (
        type(token) is not str
        or not 20 <= len(token) <= 4096
        or any(not 33 <= ord(character) <= 126 for character in token)
    ):
        raise RailwayConfigurationError("令牌格式无效：请输入完整令牌，不要包含空白或控制字符。")
    return dict(payload)


def configure(path: Path = DEFAULT_PROFILE, *, replace: bool = False) -> dict:
    """Prompt without echo; save only current-user DPAPI ciphertext, never connect."""
    try:
        if type(replace) is not bool:
            raise RailwayConfigurationError("是否覆盖必须明确指定。")
        if path.exists() and not replace:
            raise RailwayConfigurationError("已有配置，未覆盖。确需更新时请显式使用 --replace。")
        print("仅隐藏输入并加密保存本机 Railway 项目令牌；此步骤不连接云端、不部署。")
        token = hidden_input("Railway 项目令牌（输入不可见，不要粘贴到聊天）：")
        payload = _validated_payload({
            "purpose": PURPOSE,
            "project_id": TARGET_PROJECT_ID,
            "environment_id": TARGET_ENVIRONMENT_ID,
            "service_id": TARGET_SERVICE_ID,
            "token": token,
        })
        path.parent.mkdir(parents=True, exist_ok=True)
        save_secret_payload(path, payload, replace=replace)
        return _public_report("saved", cloud_connected=False, cloud_changed=False)
    except RailwayConfigurationError:
        raise
    except (EOFError, KeyboardInterrupt):
        raise RailwayConfigurationError("已取消配置，未继续保存或连接云端。") from None
    except Exception:
        raise RailwayConfigurationError(
            "未能安全隐藏输入或加密保存；请在本机交互终端重试，原始错误已隐藏。"
        ) from None


def status(path: Path = DEFAULT_PROFILE) -> dict:
    """Inspect presence only; do not read or decrypt the profile or connect."""
    try:
        exists = path.is_file()
    except Exception:
        raise RailwayConfigurationError("无法检查本机配置是否存在，原始错误已隐藏。") from None
    return _public_report("local_status", profile_exists=exists, cloud_connected="not_checked")


def load_project_payload(path: Path = DEFAULT_PROFILE) -> dict:
    """Decrypt and strictly validate the fixed project profile, without network I/O."""
    try:
        return _validated_payload(load_secret_payload(path))
    except RailwayConfigurationError:
        raise
    except Exception:
        raise RailwayConfigurationError(
            "无法读取本机加密令牌，请先配置或使用原 Windows 用户重新配置。"
        ) from None


def check(
    path: Path = DEFAULT_PROFILE, *, transport: httpx.BaseTransport | None = None
) -> dict:
    """Run the fixed identity query once; transport injection is for offline tests.

    This proves only the token's project/environment scope, not deployment state
    or service-level access. ``service_id`` in the report is the locally fixed
    intended target, not a service permission asserted by this GraphQL query.
    """
    payload = load_project_payload(path)
    try:
        with httpx.Client(
            verify=True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(connect=10, read=20, write=10, pool=10),
            transport=transport,
        ) as client, client.stream(
            "POST",
            API_URL,
            headers={
                "Project-Access-Token": payload["token"],
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
            json={"query": IDENTITY_QUERY},
        ) as response:
            if response.status_code != 200:
                raise RailwayConfigurationError(
                    "Railway 拒绝或未完成只读检查，请核对令牌与网络；未跟随重定向。"
                )
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise RailwayConfigurationError("Railway 检查响应超过安全大小，已停止。")
                raw.extend(chunk)
            result = json.loads(raw)
        if (
            type(result) is not dict
            or result.get("errors")
            or type(result.get("data")) is not dict
            or type(result["data"].get("projectToken")) is not dict
        ):
            raise RailwayConfigurationError("Railway 未返回有效的项目令牌身份，原始响应已隐藏。")
        identity = result["data"]["projectToken"]
        if (
            identity.get("projectId") != TARGET_PROJECT_ID
            or identity.get("environmentId") != TARGET_ENVIRONMENT_ID
        ):
            raise RailwayConfigurationError("令牌不属于指定项目和环境，已停止后续操作。")
        return _public_report(
            "connected_readonly",
            cloud_connected=True,
            cloud_changed=False,
            query="projectToken: projectId, environmentId",
            service_access_checked=False,
        )
    except RailwayConfigurationError:
        raise
    except (EOFError, KeyboardInterrupt):
        raise RailwayConfigurationError("已取消只读检查，未继续操作。") from None
    except Exception:
        raise RailwayConfigurationError(
            "Railway 只读检查未完成，请核对网络、证书或令牌；原始错误已隐藏。"
        ) from None
    finally:
        # Drop references; Python strings cannot be reliably physically erased.
        payload.clear()


class _SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        self.exit(2, "参数无效；仅支持 configure、status、check，不接受命令行令牌或接口地址。\n")


def main(argv=None) -> int:
    parser = _SafeParser(prog="railway_connection", description=__doc__)
    parser.add_argument("command", choices=("configure", "status", "check"))
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--replace", action="store_true", help="显式覆盖已有加密配置")
    args = parser.parse_args(argv)
    try:
        if args.replace and args.command != "configure":
            raise RailwayConfigurationError("--replace 仅能用于 configure。")
        if args.command == "configure":
            report = configure(args.profile, replace=args.replace)
        elif args.command == "check":
            report = check(args.profile)
        else:
            report = status(args.profile)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except RailwayConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print("已取消，未继续操作。", file=sys.stderr)
        return 2
    except Exception:
        print("操作未完成，原始错误已隐藏以保护令牌。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

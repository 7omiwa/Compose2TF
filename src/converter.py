"""
Docker Compose → Terraform converter core logic.
Generates HCL for the kreuzwerker/docker Terraform provider.
"""

import re
from typing import Any
import yaml

VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]+))?\}")

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _tf_name(raw: str) -> str:
    """Sanitise a string to a valid Terraform identifier."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", raw)


def _resolve_port_value(value: str) -> int:
    """
    Resolve Docker Compose variable syntax like:
        ${DB_PORT:-5432}
    """
    value = value.strip()

    # Match ${VAR:-default}
    match = re.match(r"\$\{[^:]+:-([0-9]+)\}", value)
    if match:
        return int(match.group(1))

    # Match ${VAR} — no default, cannot resolve to int
    match = re.match(r"\$\{([^}]+)\}", value)
    if match:
        raise ValueError(
            f"Environment variable '{match.group(1)}' has no default value"
        )

    return int(value)


def convert_compose_var_to_tf(value: str) -> str:
    """
    Convert:
        ${DB_NAME:-marketly}
    to:
        ${var.db_name}
    """
    if not isinstance(value, str):
        return value

    def repl(match):
        var_name = match.group(1).lower()
        return f"${{var.{var_name}}}"

    return VAR_PATTERN.sub(repl, value)


def extract_tf_variables(value: str) -> list[dict]:
    """
    Extract Terraform variables from docker-compose interpolation syntax.

    Examples:
        ${DB_NAME:-marketly_prod}  →  name=db_name, default=marketly_prod
        ${DB_PASSWORD}             →  name=db_password, default=None
    """
    results = []

    if not isinstance(value, str):
        return results

    for var_name, default in VAR_PATTERN.findall(value):
        results.append({
            "name": var_name.lower(),
            "original": var_name,
            "default": default if default else None,
        })

    return results


def _parse_port(port_str: str) -> dict:
    """
    Parse Docker Compose port syntax.

    Supports:
        "8080"
        "8080:80"
        "127.0.0.1:8080:80"
        "127.0.0.1:${DB_PORT:-5432}:5432"
        "127.0.0.1:8080:80/tcp"
    """
    proto = "tcp"
    port_str = str(port_str).strip()

    # Extract protocol suffix
    if "/" in port_str:
        port_str, proto = port_str.rsplit("/", 1)

    parts = re.split(r':(?![^${]*\})', port_str)

    ip = None

    if len(parts) == 3:
        ip, host, container = parts
    elif len(parts) == 2:
        host, container = parts
    elif len(parts) == 1:
        host = container = parts[0]
    else:
        raise ValueError(f"Invalid port format: {port_str}")

    return {
        "host": _resolve_port_value(host),
        "container": _resolve_port_value(container),
        "protocol": proto,
        "ip": ip,
    }


def _parse_env(env_val: Any) -> dict:
    """Normalise env section (list or dict) → plain dict."""
    if isinstance(env_val, dict):
        return {k: str(v) for k, v in env_val.items()}
    result = {}
    for item in (env_val or []):
        if "=" in item:
            k, v = item.split("=", 1)
            result[k] = v
        else:
            result[item] = ""   # reference to host env var
    return result


def _parse_build(build_val: Any) -> dict | None:
    """
    Normalise the compose `build:` key → dict with context/dockerfile/args,
    or None when the key is absent.

    Short form:   build: ./mydir
    Long form:    build:
                    context: .
                    dockerfile: cmd/auth/Dockerfile
                    args:
                      APP_ENV: production
    """
    if build_val is None:
        return None
    if isinstance(build_val, str):
        return {"context": build_val, "dockerfile": None, "args": {}}
    return {
        "context":    build_val.get("context", "."),
        "dockerfile": build_val.get("dockerfile"),
        "args":       build_val.get("args") or {},
    }


def _parse_volumes(vol_list: list) -> list[dict]:
    """Return list of {host, container, mode} dicts."""
    parsed = []
    for v in (vol_list or []):
        if isinstance(v, dict):          # long syntax
            parsed.append({
                "host": v.get("source", ""),
                "container": v.get("target", ""),
                "mode": "ro" if v.get("read_only", False) else "rw",
            })
        else:                            # short syntax  host:container[:mode]
            parts = str(v).split(":")
            parsed.append({
                "host": parts[0],
                "container": parts[1] if len(parts) > 1 else parts[0],
                "mode": parts[2] if len(parts) > 2 else "rw",
            })
    return parsed


# ──────────────────────────────────────────────
# Parser
# ──────────────────────────────────────────────

class ComposeParser:
    def __init__(self, compose_content: str):
        self.raw = yaml.safe_load(compose_content)
        self.version = self.raw.get("version", "3")
        self.services: dict = self.raw.get("services", {})
        self.networks: dict = self.raw.get("networks", {})
        self.volumes: dict = self.raw.get("volumes", {})

    def get_services(self) -> list[dict]:
        result = []
        for name, cfg in self.services.items():
            cfg = cfg or {}
            svc = {
                "name": name,
                "tf_name": _tf_name(name),
                "image": cfg.get("image") or (f"{name}:latest" if not cfg.get("build") else name),
                "build": _parse_build(cfg.get("build")),
                "ports": [_parse_port(p) for p in cfg.get("ports", [])],
                "environment": _parse_env(cfg.get("environment")),
                "volumes": _parse_volumes(cfg.get("volumes", [])),
                "depends_on": cfg.get("depends_on", []),
                "restart": cfg.get("restart", "no"),
                "networks": cfg.get("networks", []),
                "command": cfg.get("command"),
                "entrypoint": cfg.get("entrypoint"),
                "labels": cfg.get("labels", {}),
                "healthcheck": cfg.get("healthcheck"),
                "env_file": cfg.get("env_file", []),
            }
            result.append(svc)
        return result

    def get_named_volumes(self) -> list[str]:
        return list(self.volumes.keys())

    def get_named_networks(self) -> list[str]:
        return list(self.networks.keys())


# ──────────────────────────────────────────────
# Generators
# ──────────────────────────────────────────────

def _section_comment(title: str) -> str:
    """Render a readable section banner comment for main.tf."""
    bar = "#" + "-" * (len(title) + 4) + "#"
    return f"{bar}\n#  {title}  #\n{bar}"


class TerraformGenerator:
    """Base class – shared utilities."""

    def _hcl_string(self, v: str) -> str:
        return f'"{v}"'

    def _hcl_map(self, d: dict, indent: int = 4) -> str:
        pad = " " * indent
        lines = [f'{pad}{k} = "{v}"' for k, v in d.items()]
        return "{\n" + "\n".join(lines) + "\n" + " " * (indent - 2) + "}"

    def generate(self, parser: ComposeParser) -> str:
        raise NotImplementedError


class DockerTerraformGenerator(TerraformGenerator):
    """Generates HCL for the kreuzwerker/docker Terraform provider."""

    def __init__(self):
        self.variables: dict = {}

    # ── variable collection ───────────────────

    def collect_variables(self, parser: ComposeParser):
        """
        Walk all services and collect every ${VAR} / ${VAR:-default}
        reference found in environment values and command strings.
        """
        for svc in parser.get_services():

            # environment values
            for _, value in svc["environment"].items():
                for var in extract_tf_variables(value):
                    self.variables[var["name"]] = var

            # FIX: command block is now correctly inside the for loop
            if svc.get("command"):
                cmd = svc["command"]
                if isinstance(cmd, list):
                    cmd = " ".join(cmd)
                for var in extract_tf_variables(cmd):
                    self.variables[var["name"]] = var

    def _variable_blocks(self) -> str:
        blocks = []
        for var in self.variables.values():
            block = [
                f'variable "{var["name"]}" {{',
                "  type = string",
            ]
            if var["default"] is not None:
                block.append(f'  default = "{var["default"]}"')
            block.append("}")
            blocks.append("\n".join(block))
        return "\n\n".join(blocks)

    # ── top-level generate ────────────────────

    def generate(self, parser: ComposeParser) -> str:
        # FIX: collect variables BEFORE building variable blocks
        self.collect_variables(parser)

        blocks = [
            _section_comment("Provider"),
            self._provider_block(),
        ]

        # Variables section (only if there are any)
        if self.variables:
            blocks.append(_section_comment("Variables"))
            blocks.append(self._variable_blocks())

        # Volumes section
        named_volumes = parser.get_named_volumes()
        if named_volumes:
            blocks.append(_section_comment("Volumes"))
            for vol in named_volumes:
                blocks.append(self._volume_block(vol))

        # Networks section
        named_networks = parser.get_named_networks()
        if named_networks:
            blocks.append(_section_comment("Networks"))
            for net in named_networks:
                blocks.append(self._network_block(net))

        # Images section
        services = parser.get_services()
        blocks.append(_section_comment("Images"))
        for svc in services:
            blocks.append(self._image_block(svc))

        # Containers section
        blocks.append(_section_comment("Containers"))
        for svc in services:
            blocks.append(self._container_block(svc, named_networks))

        return "\n\n".join(blocks)

    # ── provider ──────────────────────────────

    def _provider_block(self) -> str:
        return '''\
terraform {
  required_providers {
    docker = {
      source  = "kreuzwerker/docker"
      version = "~> 3.0"
    }
  }
}

provider "docker" {
  host = "unix:///var/run/docker.sock"
}'''

    # ── volume ────────────────────────────────

    def _volume_block(self, name: str) -> str:
        tf = _tf_name(name)
        return f'''\
resource "docker_volume" "{tf}" {{
  name = "{name}"
}}'''

    # ── network ───────────────────────────────

    def _network_block(self, name: str) -> str:
        tf = _tf_name(name)
        return f'''\
resource "docker_network" "{tf}" {{
  name = "{name}"
}}'''

    # ── image ─────────────────────────────────

    def _image_block(self, svc: dict) -> str:
        tf = svc["tf_name"]
        build = svc.get("build")

        if build:
            # Locally built image — emit a build {} block so Terraform
            # builds the image from source instead of pulling from a registry.
            # keep_locally = true prevents Terraform from destroying it on down.
            lines = [f'resource "docker_image" "{tf}" {{']
            lines.append(f'  name         = "{svc["image"]}"')
            lines.append( '  keep_locally = true')
            lines.append( '  build {')
            lines.append(f'    context    = "{build["context"]}"')
            if build["dockerfile"]:
                lines.append(f'    dockerfile = "{build["dockerfile"]}"')
            if build["args"]:
                lines.append( '    build_arg {')
                for k, v in build["args"].items():
                    v = convert_compose_var_to_tf(str(v))
                    lines.append(f'      {k} = "{v}"')
                lines.append( '    }')
            lines.append( '  }')
            lines.append( '}')
            return "\n".join(lines)

        return f'''\
resource "docker_image" "{svc['tf_name']}" {{
  name         = "{svc['image']}"
  keep_locally = false
}}'''

    # ── container ─────────────────────────────

    def _container_block(self, svc: dict, all_networks: list[str]) -> str:
        tf = svc["tf_name"]
        lines = [f'resource "docker_container" "{tf}" {{']
        lines.append(f'  name  = "{svc["name"]}"')
        lines.append(f'  image = docker_image.{tf}.image_id')

        if svc["restart"] and svc["restart"] != "no":
            lines.append(f'  restart = "{svc["restart"]}"')

        if svc["command"]:
            cmd = svc["command"] if isinstance(svc["command"], list) else [svc["command"]]
            cmd_str = ", ".join(f'"{c}"' for c in cmd)
            lines.append(f'  command = [{cmd_str}]')

        # env vars
        if svc["environment"]:
            lines.append("  env = [")
            for k, v in svc["environment"].items():
                v = convert_compose_var_to_tf(v)
                lines.append(f'    "{k}={v}",')
            lines.append("  ]")

        # ports
        for p in svc["ports"]:
            lines.append("  ports {")
            lines.append(f'    internal = {p["container"]}')
            lines.append(f'    external = {p["host"]}')
            if p.get("ip"):
                lines.append(f'    host_ip  = "{p["ip"]}"')
            lines.append(f'    protocol = "{p["protocol"]}"')
            lines.append("  }")

        # volumes
        for v in svc["volumes"]:
            lines.append("  volumes {")
            host = v["host"]
            if host.startswith("/"):
                # Already absolute — use as-is
                lines.append(f'    host_path      = "{host}"')
            elif host.startswith("."):
                # Relative path — resolve against the Terraform module directory
                # at plan time so the Docker provider receives an absolute path.
                if host == ".":
                    rel = ""
                elif host.startswith("./"):
                    rel = host[2:]
                else:
                    rel = host[1:].lstrip("/")
                if rel:
                    lines.append(f'    host_path      = abspath("${{path.module}}/{rel}")')
                else:
                    lines.append(f'    host_path      = abspath(path.module)')
            else:
                lines.append(f'    volume_name    = docker_volume.{_tf_name(host)}.name')
            lines.append(f'    container_path = "{v["container"]}"')
            if v["mode"] == "ro":
                lines.append("    read_only      = true")
            lines.append("  }")

        # networks
        nets = svc["networks"]
        if isinstance(nets, dict):   # long-form networks: {mynet: {aliases: [...]}}
            nets = list(nets.keys())
        if not nets:
            nets = all_networks
        for net in nets:
            lines.append("  networks_advanced {")
            if net in all_networks:
                # Declared in the compose file — reference the managed resource
                lines.append(f'    name = docker_network.{_tf_name(net)}.name')
            else:
                # Built-in or external network (e.g. "default", "host", "bridge")
                # — not managed by this Terraform module, use a plain string
                lines.append(f'    name = "{net}"')
            lines.append("  }")

        # depends_on
        if svc["depends_on"]:
            deps_raw = svc["depends_on"]
            deps = deps_raw if isinstance(deps_raw, list) else list(deps_raw.keys())
            dep_str = ", ".join(f"docker_container.{_tf_name(d)}" for d in deps)
            lines.append(f"  depends_on = [{dep_str}]")

        # healthcheck
        hc = svc.get("healthcheck")
        if hc and isinstance(hc, dict) and hc.get("test"):
            test = hc["test"]
            if isinstance(test, list):
                test = test[1:] if test[0] in ("CMD", "CMD-SHELL") else test
                test_str = ", ".join(f'"{t}"' for t in test)
            else:
                test_str = f'"{test}"'
            lines.append("  healthcheck {")
            lines.append(f"    test         = [{test_str}]")
            if hc.get("interval"):
                lines.append(f'    interval     = "{hc["interval"]}"')
            if hc.get("timeout"):
                lines.append(f'    timeout      = "{hc["timeout"]}"')
            if hc.get("retries"):
                lines.append(f'    retries      = {hc["retries"]}')
            lines.append("  }")

        lines.append("}")
        return "\n".join(lines)

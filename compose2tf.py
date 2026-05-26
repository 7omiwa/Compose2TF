#!/usr/bin/env python3
"""
compose2tf — Convert docker-compose.yml → Terraform HCL (Docker provider)

Usage:
    python compose2tf.py -f docker-compose.yml -f docker-compose.prod.yml [options]

Options:
    -f, --file     Compose file (multiple allowed, later overrides earlier)
    -o, --output   Output .tf file (default: main.tf)
    --tfvars       Output .tfvars file path (auto-detected for *.prod.yml / *.override.yml)
"""

import argparse
import sys
from pathlib import Path
from typing import List, Dict, Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))

# FIX: import VAR_PATTERN and extract_tf_variables from converter where they live
from src.converter import (
    ComposeParser,
    DockerTerraformGenerator,
    VAR_PATTERN,
    extract_tf_variables,
)

BANNER = r"""
  ____                                  ____  _____  __
 / ___|___  _ __ ___  _ __   ___  _____|___ \|_   _|/ _|
| |   / _ \| '_ ` _ \| '_ \ / _ \/ __|  __) | | | | |_
| |__| (_) | | | | | | |_) | (_) \__ \ / __/  | | |  _|
 \____\___/|_| |_| |_| .__/ \___/|___/|_____| |_| |_|
                      |_|
  docker-compose → Terraform (Docker provider) converter
"""

# Environment suffixes that indicate a prod/override file whose values
# should be written out to a .tfvars file automatically.
_PROD_SUFFIXES = (".prod.yml", ".prod.yaml", ".override.yml", ".override.yaml", ".staging.yml", ".staging.yaml")


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Simple recursive merge following Docker Compose rules (approximate)."""
    result = base.copy()
    for key, value in override.items():
        if key not in result:
            result[key] = value
        elif isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = _deep_merge(result[key], value)
        elif isinstance(value, list) and isinstance(result[key], list):
            # Append for lists like ports, volumes; no duplicates
            result[key] = result[key] + [item for item in value if item not in result[key]]
        else:
            result[key] = value
    return result


def merge_compose_files(files: List[Path]) -> Dict[str, Any]:
    """Merge multiple compose files into one dict."""
    merged = {}
    for file_path in files:
        print(f"📄  Reading  : {file_path}")
        content = file_path.read_text()
        data = yaml.safe_load(content) or {}
        merged = _deep_merge(merged, data)
    return merged


def _is_prod_file(path: Path) -> bool:
    """Return True when the filename looks like a prod/override/staging compose file."""
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _PROD_SUFFIXES)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Convert docker-compose files to Terraform HCL (Docker provider)",
    )
    parser.add_argument(
        "-f", "--file",
        action="append",
        dest="compose_files",
        required=True,
        help="Path to docker-compose file(s). Can be used multiple times. Later files override.",
    )
    parser.add_argument(
        "-o", "--output",
        default="main.tf",
        help="Output .tf file (default: main.tf)",
    )
    parser.add_argument(
        "--tfvars",
        default=None,
        help="Output .tfvars file. Auto-detected from prod/override filenames when omitted.",
    )
    parser.add_argument(
        "--no-banner",
        action="store_true",
        help="Suppress the ASCII banner",
    )
    return parser.parse_args()


def generate_tfvars(parser: ComposeParser) -> str:
    """
    Build a terraform.tfvars snippet from:
      - restart policies per service
      - every ${VAR:-default} / ${VAR} found in environment values
    """
    lines = []

    for svc in parser.get_services():

        restart = svc.get("restart")
        if restart and restart != "no":
            lines.append(
                f'{svc["name"].replace("-", "_")}_restart = "{restart}"'
            )

        for _, value in svc["environment"].items():
            for var in extract_tf_variables(value):
                if var["default"]:
                    lines.append(f'{var["name"]} = "{var["default"]}"')
                else:
                    lines.append(f'{var["name"]} = "CHANGE_ME"')

    return "\n".join(sorted(set(lines)))


def main():
    args = parse_args()

    if not args.no_banner:
        print(BANNER)

    compose_paths = [Path(f) for f in args.compose_files]

    # Auto-detect tfvars output when a prod/override file is present
    tfvars_output = args.tfvars
    if tfvars_output is None:
        prod_files = [p for p in compose_paths if _is_prod_file(p)]
        if prod_files:
            tfvars_output = "terraform.tfvars"
            print(f"🔍  Prod/override file detected ({prod_files[0].name}) — will auto-generate: {tfvars_output}")

    # Merge all compose files
    merged_data = merge_compose_files(compose_paths)

    # Save merged for debugging
    merged_path = Path("merged-compose.yml")
    merged_path.write_text(
        yaml.safe_dump(merged_data, sort_keys=False, default_flow_style=False, indent=2)
    )
    print(f"🔄  Merged config saved to: {merged_path}")

    try:
        merged_yaml_str = yaml.safe_dump(merged_data)
        compose_parser = ComposeParser(merged_yaml_str)
        services = compose_parser.get_services()
        print(f"🐳  Services : {', '.join(s['name'] for s in services)}")
        if compose_parser.get_named_volumes():
            print(f"💾  Volumes  : {', '.join(compose_parser.get_named_volumes())}")
        if compose_parser.get_named_networks():
            print(f"🌐  Networks : {', '.join(compose_parser.get_named_networks())}")
    except Exception as e:
        print(f"❌  Failed to parse merged compose: {e}", file=sys.stderr)
        sys.exit(1)

    generator = DockerTerraformGenerator()

    try:
        terraform_hcl = generator.generate(compose_parser)
    except Exception as e:
        print(f"❌  Failed to generate Terraform: {e}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    output_path.write_text(terraform_hcl)
    print(f"\n✅  Written Terraform: {output_path} ({len(terraform_hcl):,} bytes)")

    if tfvars_output:
        tfvars_path = Path(tfvars_output)
        tfvars_content = generate_tfvars(compose_parser)
        tfvars_path.write_text(tfvars_content)
        print(f"📋  tfvars template written to: {tfvars_path}")
        print(f"    Use with: terraform apply -var-file={tfvars_path}")

    print(f"\n💡  Next steps:")
    print(f"    terraform init")
    print(f"    terraform plan")
    print(f"    terraform apply")


if __name__ == "__main__":
    main()
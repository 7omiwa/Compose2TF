# Compose2TF

This program Converts `docker-compose.yml` files to Terraform HCL using the [kreuzwerker/docker](https://registry.terraform.io/providers/kreuzwerker/docker/latest/docs) provider.

## Requirements

- Python 3.10+
- PyYAML

```
pip install -r requirements.txt
```

## Project structure

```
│-  compose2tf.py
│-  docker-compose.yml # its Recommended your Docker-Compose sit here but you can adjust in the Run command to specify dir 
│-  README.md
│-  requirements.txt
│
└───src
    │   converter.py
    │
    └───__pycache_*
```

## Usage

### Basic

```bash
python compose2tf.py -f docker-compose.yml
```

Outputs `main.tf` in the current directory.

### With an override or prod file

Pass multiple `-f` flags. Files are merged left to right — later files override earlier ones, the same way Docker Compose handles them.

```bash
python compose2tf.py -f docker-compose.yml -f docker-compose.prod.yml
```

When a file matching `*.prod.yml`, `*.prod.yaml`, `*.override.yml`, `*.override.yaml`, `*.staging.yml`, or `*.staging.yaml` is detected, a `terraform.tfvars` file is generated automatically.

### All options

```bash
python compose2tf.py \
  -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -o main.tf \
  --tfvars prod.tfvars \
  --no-banner
```

| Flag | Required | Default | Description |
|---|---|---|---|
| `-f`, `--file` | Yes | — | Path to a compose file. Repeat for multiple files. |
| `-o`, `--output` | No | `main.tf` | Output path for the generated Terraform file. |
| `--tfvars` | No | auto-detected | Path to write a `.tfvars` template. Auto-set to `terraform.tfvars` when a prod/override file is present. |
| `--no-banner` | No | off | Suppress the ASCII art banner. |

## What gets converted

### Services

Each service becomes a `docker_image` resource and a `docker_container` resource.

Supported fields:

- `image`
- `build` — short form (`build: ./dir`) and long form (`context`, `dockerfile`, `args`)
- `ports` — short, host-bound (`127.0.0.1:8080:80`), and variable syntax (`${PORT:-8080}`)
- `environment` — list and dict forms
- `volumes` — short, long, named, host-path, and read-only
- `networks`
- `depends_on` — list and service-condition map forms
- `restart`
- `command`
- `healthcheck`

### Volumes

Top-level named volumes become `docker_volume` resources and are referenced by name from containers.

### Networks

Top-level named networks become `docker_network` resources.

### Environment variable interpolation

Compose variables are converted to Terraform input variables:

| Compose | Terraform |
|---|---|
| `${DB_NAME:-marketly}` | `var.db_name` with `default = "marketly"` |
| `${DB_PASSWORD}` | `var.db_password` with no default |

A `variable` block is generated for every unique variable found across all environment values and command strings.

### Build context

Services with a `build:` block get a `build {}` block in their `docker_image` resource instead of a registry pull. `keep_locally` is set to `true` so Terraform does not destroy locally built images on teardown.

```hcl
resource "docker_image" "auth" {
  name         = "auth"
  keep_locally = true
  build {
    context    = "."
    dockerfile = "cmd/auth/Dockerfile"
  }
}
```

## Output files

| File | Description |
|---|---|
| `main.tf` | Generated Terraform configuration (or whatever `-o` is set to). |
| `terraform.tfvars` | Variable values template, generated when a prod/override file is passed or `--tfvars` is set. Fill in any `"CHANGE_ME"` values before applying. |
| `merged-compose.yml` | The result of merging all input files. Useful for debugging unexpected output. |

## After conversion

```bash
terraform init
terraform plan
terraform apply

# With a tfvars file
terraform apply -var-file=prod.tfvars
```

## Limitations

- The Docker provider manages containers on the machine where Terraform runs. This tool is suited for single-host deployments, not multi-host orchestration (use ECS, Kubernetes, or Nomad resources for that).
- `env_file:` references are noted but not inlined — values loaded from `.env` files at runtime are not visible to the converter.
- `deploy:` (Swarm mode) fields are ignored.
- All generated variable types are `string`. If your Terraform configuration needs numeric or boolean variables, adjust the generated blocks manually.


⚠️ Important Notice

Before applying any generated Terraform configuration, always review the output carefully.

This converter automates translation from Docker Compose to Terraform, but generated infrastructure code should not be treated as automatically safe or production-ready without verification.

It is strongly recommended to:
```
terraform init
terraform plan -out=tfplan
```
Carefully review the generated execution plan before running:
```
terraform apply tfplan
```
Do not rely entirely on the authenticity or correctness of automatically generated Terraform files. Always validate networking, environment variables, volume mappings, exposed ports, secrets, and deployment behavior before applying changes to any environment.

📜 License & Attribution

This project is licensed under the GNU GPL License.
If you use, modify, distribute, or build upon this codebase, proper attribution is appreciated.
```
Author: Olanipekun Emmanuel Tomiwa
Email: mephisto.sting@yahoo.com
GitHub Repository: Compose2TF Repository
```

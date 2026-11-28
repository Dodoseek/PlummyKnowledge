# Local models

PlummyKnowledge can use a model server inside your network: Ollama, or any server with an OpenAI-compatible API
(vLLM, LM Studio, llama.cpp server and others). Documents and questions then never leave your infrastructure.

The application refuses connections to private addresses by default, so a local model takes two things: a network
path from the `backend` and `worker` containers, and an allowlist entry for exactly that address.

## Ollama in the same Compose project

Create an override file next to `compose.prod.yaml`, for example `compose.llm.yaml`:

```yaml
services:
  ollama:
    image: ollama/ollama:<version>   # pin a version
    volumes: [ollama-data:/root/.ollama]
    networks: [llm]
    restart: unless-stopped
  backend:
    networks: [llm]
  worker:
    networks: [llm]

networks:
  llm:
    internal: true
    ipam:
      config:
        - subnet: 172.30.210.0/24

volumes:
  ollama-data: {}
```

The `llm` network is internal: the model server has no internet access and no published port, and only
`backend` and `worker` can reach it. The fixed subnet is what the allowlist entry refers to.

Allow it in `deploy/prod/plummy.toml` (keep the existing `bundled-s3` entry):

```toml
[[network.allowlist]]
name           = "local-ollama"
url            = "http://ollama:11434"
purposes       = ["llm", "embedding"]
provider_types = ["ollama"]
allowed_cidrs  = ["172.30.210.0/24"]
```

Start the stack with both files:

```bash
docker compose -f compose.prod.yaml -f compose.llm.yaml --env-file deploy/prod/.env --profile s3 up -d --wait
```

Pass the same `-f` options to every later `docker compose` command, or set
`COMPOSE_FILE=compose.prod.yaml:compose.llm.yaml` in the shell.

### Pulling models

Because `llm` has no internet access, pull models with a temporary container on the default network that uses the
same volume (`docker volume ls` shows its full name, `<project>_ollama-data`):

```bash
docker run --rm -v plummy_ollama-data:/root/.ollama --entrypoint sh ollama/ollama:<version> \
  -c 'ollama serve & sleep 5 && ollama pull <chat model> && ollama pull <embedding model>'
```

## A model server elsewhere

For a server on another host in your network, no override is needed if the host is reachable from the `egress`
network. Add an allowlist entry with its URL and the narrowest CIDR, for example its single address
`10.0.5.20/32`. A server on the Docker host itself (`host.docker.internal`) needs a host-gateway mapping on Linux
and the CIDR of the Docker bridge gateway.

## In the web app

1. **AI providers → Add**: type `Ollama` (or `OpenAI-compatible`) and the same base URL as in the allowlist, for
   example `http://ollama:11434`. **Test connection**.
2. Add a chat model, and an embedding profile on an embedding model. The profile's dimensions must match the
   model.
3. Use them in an assistant.

`endpoint_not_allowed` or `egress_denied` means the URL, the purpose or the CIDR of the allowlist entry does not
match: compare them with the address the host name actually resolves to inside the container.

# Guides

Guides for people who install and run PlummyKnowledge, and for developers who extend it. They describe the
production stack `compose.prod.yaml`; the reference behind them is [DEPLOYMENT.md](../DEPLOYMENT.md) (in
Russian), which wins if the two ever disagree.

## Operators

| Guide | When you need it |
|---|---|
| [Installation](installation.md) | First install on a server: secrets, TLS, storage, first administrator |
| [Configuration](configuration.md) | Environment variables, the instance configuration file, limits |
| [Local models](local-models.md) | Ollama or another OpenAI-compatible server inside your network |
| [Connectors](connectors.md) | Google Drive and Microsoft 365 |
| [Upgrade](upgrade.md) | Moving to a new release |
| [Backup and restore](backup-restore.md) | Backups, restore, deletions made after a backup |
| [Operations](operations.md) | Scaling, key rotation, audit retention, the server CLI |
| [Troubleshooting](troubleshooting.md) | Symptoms and what to check |

Contributions to the code and to these guides are welcome: see [CONTRIBUTING.md](../../CONTRIBUTING.md).

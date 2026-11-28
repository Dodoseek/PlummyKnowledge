# Security policy

PlummyKnowledge is self-hosted: each installation is deployed and run by its owner. The threat model, trust
boundaries and required checks are in [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) (in Russian).

## Supported versions

| Version | Security fixes |
|---|---|
| The latest release (currently 0.5.x) | Yes |
| Earlier releases (0.4.x, 0.1.x) | No: upgrade to the latest release ([upgrade guide](docs/guides/upgrade.md)) |
| Builds between releases (`X.Y.Z-dev.N`) | No: for development and testing |

From 1.0.0, security fixes are published for the latest minor release, and the public contract follows Semantic
Versioning ([ADR-025](docs/adr/0025-semver-and-contract-stability.md)). A fix that cannot be made without changing
the contract is released anyway and described under `Security` in [CHANGELOG.md](CHANGELOG.md). Before 1.0.0 the
contract may change between minor versions.

The production profile is `compose.prod.yaml`: Nginx with TLS and CSP, secrets as files, segmented networks
([installation guide](docs/guides/installation.md)).

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion.** Report privately, either through
[GitHub private vulnerability reporting](https://github.com/Dodoseek/PlummyKnowledge/security/advisories/new) or
by email to **developer@plummycode.ru**, with:

- the version (the `VERSION` file, or the version shown in the web app's header) and how it is deployed;
- what the problem is and its impact (which data or permissions are affected);
- steps to reproduce or a proof of concept;
- known workarounds, if any.

Do not include real secrets, tokens, passwords or other people's documents. We acknowledge every report, agree
on a timeline for the fix and the disclosure with you, and credit you in the release notes if you wish.

Сообщения на русском тоже принимаются.

## What counts as a vulnerability

For example: bypassing authorization or data isolation (access to another user's knowledge base, document,
conversation or session), passages the user may not read reaching a model, CSRF or XSS, SSRF around the network
policy, disclosure of provider secrets or connector tokens, an uploaded file escaping storage, bypassing the
first-run setup.

Not vulnerabilities of the project: configuration problems of a particular installation (ports exposed, weak
passwords, TLS disabled contrary to the [guides](docs/guides/README.md)); vulnerabilities in dependencies without
a demonstrated impact on PlummyKnowledge (report those to the dependency's authors); model answers that stay
within the data the user may read.

## How dependencies and images are checked

- **Dependencies:** `pip-audit` (`uv.lock`) and `npm audit` on every push and pull request, dependency review of
  new dependencies in pull requests, and Dependabot for Python, npm, GitHub Actions, Docker and Compose
  ([ci.yml](.github/workflows/ci.yml)).
- **Images:** Trivy (pinned by version and digest) scans every image of the production stack: the project's own
  `backend`, `worker-ingestion` and `web` images and the third-party PostgreSQL with pgvector, Redis and S3 server
  images. CI fails on a CRITICAL or HIGH vulnerability that has a fixed version. Own images install the base
  image's security updates at build time and contain no pip. Exceptions are allowed only for third-party images,
  in `deploy/trivy/<image>.trivyignore`, each with a reason and an expiry date at most a year away, after which the
  finding fails CI again ([SECURITY_MODEL.md](docs/SECURITY_MODEL.md) §19).
- To check your own installation, run `sh deploy/scripts/scan-images.sh` on the server after `docker compose pull`
  and `docker compose build` (it needs Docker and access to the Trivy vulnerability database).

# Nailgun

Nailgun is a self-hosted outbound mail gateway. It accepts authenticated REST, Mailgun-compatible form submissions and MCP requests, and keeps a durable MariaDB queue for delivery through a TLS SMTP relay.

An accepted request returns HTTP 202 and a queue ID. Reuse an idempotency key to retry the same logical submission. Reusing the key with a different payload returns HTTP 409. The authenticated status API returns metadata and omits message bodies. Use the raw MIME endpoint for attachments. A complete MIME message can be up to 5 MiB before base64 encoding.

## Run the gateway

1. Install Python 3.12 or later and MariaDB. Use a local Unix socket and a dedicated database account.
2. Run `uv sync --locked`. Provision the schema and its queue lock with `python deploy/migrate.py provision nailgun deploy/schema.sql /etc/vps-mail/mariadb.password` as a MariaDB administrator authenticated over the default Unix socket. Make the credential readable only by the service account.
3. Copy `.env.example` to a private configuration file. Set a randomly generated API key, the public hostname and your SMTP relay settings. The API key permits sending, viewing and canceling messages, and configuring sender domains.
4. Run `uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8893` behind a TLS proxy. Example systemd units and an Nginx location snippet are in `deploy/`. Adjust paths and identities to your installation. Enable only verified sender domains.

Use `POST /v1/messages` for structured mail and `POST /v1/messages/raw` for MIME. Authenticate with `Authorization: Bearer <key>`. MCP is served at `/mcp/`. The Mailgun-compatible `/v3/<domain>/messages` endpoint also accepts Basic authentication with username `api` and the gateway key as the password.

## Local submissions and WordPress

On Linux, the local broker uses Unix socket peer credentials to verify the operating-system account. Configure the allowed users and sender domains in the broker configuration. Install `integrations/sendmail.py` as `/usr/local/sbin/vps-sendmail` and grant submission socket access only to authorized accounts. The `--check` option validates a submission without queuing it.

The [VPS Mail Gateway plugin](wordpress/vps-mail-gateway) supports WordPress 6.8 or newer. Local mode uses the submission handler. API mode sends to the configured HTTPS endpoint and binds the API key to that endpoint. WordPress receives success once the queue accepts the message. SMTP delivery happens asynchronously.

## Upgrade or restore

Stop API, local submissions, and the delivery worker before importing a SQLite queue. Back up the stopped queue, provision an empty database, and run `python deploy/migrate.py import nailgun /absolute/path/to/queue.sqlite3`. The importer reconciles each copied row. Keep the original snapshot until both services and their message counts are verified. Back up the MariaDB schema and configuration separately, with access limited to the operator.

## Development

Set `NAILGUN_TEST_SOCKET` to an isolated development MariaDB server, then run `uv run --locked pytest -q`. The tests create and remove their own disposable databases and mock SMTP. Run `uv run --locked ruff check app tests` for static checks.

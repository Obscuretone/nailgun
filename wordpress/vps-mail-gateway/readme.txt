=== VPS Mail Gateway ===
Requires at least: 6.8
Requires PHP: 8.0
Stable tag: 1.0.0
License: MIT

Queues WordPress email through VPS Mail while preserving WordPress mail hooks, MIME attachments, HTML alternatives, Reply-To, Cc and Bcc.

== Installation ==
Upload the ZIP or the vps-mail-gateway folder under wp-content/plugins and activate it. Configure Settings > VPS Mail. Use only one mail transport plugin.

On the VPS choose Local sendmail handler. The system administrator must install /usr/local/sbin/vps-sendmail and authorize the PHP pool user in /etc/vps-mail/local.json and the vps-mail-submit group. No API key is given to a local website.

For an external WordPress host choose REST API and enter your gateway base URL, such as https://mail.example.com. Save a new key whenever the endpoint changes. In wp-config.php define VPS_MAIL_ENDPOINT and VPS_MAIL_API_KEY together. Other supported constants include VPS_MAIL_TRANSPORT, VPS_MAIL_FROM, and VPS_MAIL_NAME. Constants override the settings screen.

The configured From address overrides other senders so contact forms cannot spoof a visitor's address. Put the visitor address in Reply-To. Confirm that the sender domain is enabled in VPS Mail.

== Delivery ==
A successful wp_mail call means the message was accepted into the queue. Google SMTP delivery and recipient inbox placement happen later. Check the gateway's delivery records. An API or local queue refusal returns failure through WordPress's normal wp_mail_failed hook. There is no fallback to another SMTP service. Complete MIME messages may be at most 5 MiB, including attachment encoding overhead.

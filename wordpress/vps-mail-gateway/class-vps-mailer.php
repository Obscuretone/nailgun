<?php
defined('ABSPATH') || exit;

class VPS_Mail_Gateway_Mailer extends WP_PHPMailer {
    public string $gateway_error = '';
    public function send() {
        if ($this->gateway_error !== '') {
            throw new \PHPMailer\PHPMailer\Exception($this->gateway_error);
        }
        return parent::send();
    }

    public function postSend() {
        if ($this->Mailer !== 'vpsmail') { return parent::postSend(); }
        $settings = vps_mail_settings();
        $endpoint = vps_mail_api_endpoint($settings['endpoint']);
        if (!$endpoint || !$settings['api_key']) {
            throw new \PHPMailer\PHPMailer\Exception('VPS Mail API requires an HTTPS endpoint and API key.');
        }
        $mime = $this->getSentMIMEMessage();
        if (strlen($mime) > 5 * 1024 * 1024) {
            throw new \PHPMailer\PHPMailer\Exception('VPS Mail message exceeds the 5 MiB MIME limit.');
        }
        $recipients = array_merge($this->getToAddresses(), $this->getCcAddresses(), $this->getBccAddresses());
        $payload = [
            'from' => $this->Sender ?: $this->From,
            'to' => array_values(array_unique(array_column($recipients, 0))),
            'raw' => base64_encode($mime),
        ];
        $response = wp_remote_post($endpoint . '/v1/messages/raw', [
            'timeout' => 30, 'redirection' => 0,
            'headers' => [
                'Authorization' => 'Bearer ' . $settings['api_key'],
                'Content-Type' => 'application/json',
                'Idempotency-Key' => 'wordpress-' . hash('sha256', $this->getLastMessageID()),
            ],
            'body' => wp_json_encode($payload),
        ]);
        if (is_wp_error($response)) {
            throw new \PHPMailer\PHPMailer\Exception('VPS Mail API did not acknowledge the submission.');
        }
        $status = wp_remote_retrieve_response_code($response);
        $body = json_decode(wp_remote_retrieve_body($response), true);
        if ($status !== 202 || empty($body['id'])) {
            throw new \PHPMailer\PHPMailer\Exception('VPS Mail API refused the submission (HTTP ' . (int) $status . ').');
        }
        return true;
    }
}

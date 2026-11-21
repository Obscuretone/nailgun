<?php
// Run with: php -d sendmail_path=/usr/bin/false tests/wordpress-offline.php /path/to/wordpress
if (PHP_SAPI !== 'cli' || !isset($argv[1]) || !is_file($argv[1] . '/wp-includes/class-wp-phpmailer.php')) {
    throw new RuntimeException('Provide an isolated WordPress source tree.');
}
if (ini_get('sendmail_path') !== '/usr/bin/false') {
    throw new RuntimeException('Disable real sendmail before running this test.');
}
define('ABSPATH', rtrim($argv[1], '/') . '/');
define('WPINC', 'wp-includes');
$settings = ['transport' => 'api', 'endpoint' => 'https://mail.example.com', 'api_key' => 'synthetic-fixture-key'];
$requests = [];
$hooks = [];
function add_filter($name, $handler, $priority = 10) { $GLOBALS['hooks'][$name] = $handler; }
function add_action($name, $handler, $priority = 10) { add_filter($name, $handler, $priority); }
function get_option($name, $default = []) { return $GLOBALS['settings']; }
function wp_parse_args($value, $defaults) { return array_replace($defaults, $value); }
function wp_parse_url($url, $component = -1) { return parse_url($url, $component); }
function home_url() { return 'https://site.example.com'; }
function get_bloginfo($key) { return 'Test site'; }
function __($text) { return $text; }
function wp_specialchars_decode($text, $quotes) { return html_entity_decode($text, $quotes); }
function is_email($value) { return filter_var($value, FILTER_VALIDATE_EMAIL); }
function wp_json_encode($value) { return json_encode($value, JSON_THROW_ON_ERROR); }
function wp_remote_post($url, $options) {
    $GLOBALS['requests'][] = [$url, $options];
    return ['code' => 202, 'body' => '{"id":"synthetic"}'];
}
function is_wp_error($value) { return false; }
function wp_remote_retrieve_response_code($value) { return $value['code']; }
function wp_remote_retrieve_body($value) { return $value['body']; }
function require_condition($condition, $message) {
    if (!$condition) { throw new RuntimeException($message); }
}

require __DIR__ . '/../wordpress/vps-mail-gateway/vps-mail-gateway.php';
$hooks['wp_mail']([]);
$phpmailer->addAddress('MixedCase@example.com');
$phpmailer->addBCC('BlindCopy@example.net');
$phpmailer->Subject = 'Offline fixture';
$phpmailer->Body = 'Synthetic message';
$hooks['phpmailer_init']($phpmailer);
require_condition($phpmailer->send(), 'The synthetic API request was not queued.');
[$url, $options] = $requests[0];
$payload = json_decode($options['body'], true, 512, JSON_THROW_ON_ERROR);
require_condition($url === 'https://mail.example.com/v1/messages/raw' && $options['redirection'] === 0, 'Endpoint must stay pinned.');
require_condition($payload['to'] === ['MixedCase@example.com', 'BlindCopy@example.net'], 'Envelope addresses changed.');

$phpmailer = new WP_PHPMailer(true);
$hooks['phpmailer_init']($phpmailer);
try {
    $phpmailer->send();
    throw new RuntimeException('A conflicting mailer did not fail closed.');
} catch (\PHPMailer\PHPMailer\Exception $error) {
    require_condition(str_contains($error->getMessage(), 'conflicting plugin'), 'Unexpected transport error.');
}
require_condition(count($requests) === 1, 'The conflicting mailer sent a request.');

define('VPS_MAIL_API_KEY', 'synthetic-constant-key');
require_condition(vps_mail_settings()['api_key'] === '', 'A constant key requires a pinned endpoint.');
define('VPS_MAIL_ENDPOINT', 'https://fixed.example.com');
$pinned = vps_mail_settings();
require_condition($pinned['endpoint'] === 'https://fixed.example.com' && $pinned['api_key'] === VPS_MAIL_API_KEY, 'The configured pair was not accepted.');
echo "Offline WordPress gateway checks passed\n";

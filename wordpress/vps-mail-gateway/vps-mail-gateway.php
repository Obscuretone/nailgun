<?php
/**
 * Plugin Name: VPS Mail Gateway
 * Description: Queue WordPress email through VPS Mail using the local sendmail handler or the authenticated REST API.
 * Version: 1.0.0
 * Requires at least: 6.8
 * Requires PHP: 8.0
 * License: MIT
 */
defined('ABSPATH') || exit;

function vps_mail_api_endpoint($value): string {
    if (!is_string($value) || $value === '' || preg_match('/[\x00-\x20\x7f]/', $value)) { return ''; }
    $parts = wp_parse_url($value);
    if (!$parts || ($parts['scheme'] ?? '') !== 'https' || empty($parts['host']) ||
        array_intersect(['user', 'pass', 'query', 'fragment'], array_keys($parts))) { return ''; }
    return rtrim($value, '/');
}

function vps_mail_settings(): array {
    $saved = get_option('vps_mail_gateway', []);
    $settings = wp_parse_args(is_array($saved) ? $saved : [], [
        'enabled' => true, 'transport' => 'local', 'endpoint' => '', 'api_key' => '',
        'from' => 'wordpress@' . preg_replace('/^www\./', '', (string) wp_parse_url(home_url(), PHP_URL_HOST)),
        'name' => wp_specialchars_decode(get_bloginfo('name'), ENT_QUOTES),
    ]);
    $saved_endpoint = vps_mail_api_endpoint($settings['endpoint']);
    $settings['endpoint'] = defined('VPS_MAIL_ENDPOINT') ? vps_mail_api_endpoint(VPS_MAIL_ENDPOINT) : $saved_endpoint;
    if (defined('VPS_MAIL_API_KEY')) {
        $settings['api_key'] = defined('VPS_MAIL_ENDPOINT') && $settings['endpoint'] !== '' ? VPS_MAIL_API_KEY : '';
    } elseif ($settings['endpoint'] === '' || $settings['endpoint'] !== $saved_endpoint) {
        $settings['api_key'] = '';
    }
    foreach (['transport', 'from', 'name'] as $field) {
        $constant = 'VPS_MAIL_' . strtoupper($field);
        if (defined($constant)) { $settings[$field] = constant($constant); }
    }
    return $settings;
}

// Install the custom mailer before core populates recipients, headers and MIME.
// Core still runs its normal filters, attachment handling, wp_mail_failed and
// wp_mail_succeeded hooks. No pre_wp_mail short-circuit is used.
add_filter('wp_mail', function ($attributes) {
    if (!vps_mail_settings()['enabled']) { return $attributes; }
    require_once ABSPATH . WPINC . '/PHPMailer/Exception.php';
    require_once ABSPATH . WPINC . '/PHPMailer/PHPMailer.php';
    require_once ABSPATH . WPINC . '/PHPMailer/SMTP.php';
    require_once ABSPATH . WPINC . '/class-wp-phpmailer.php';
    require_once __DIR__ . '/class-vps-mailer.php';
    global $phpmailer;
    if (!($phpmailer instanceof VPS_Mail_Gateway_Mailer)) {
        $phpmailer = new VPS_Mail_Gateway_Mailer(true);
        $phpmailer::$validator = 'is_email';
    }
    return $attributes;
}, -10000);

add_action('phpmailer_init', function ($mailer) {
    $settings = vps_mail_settings();
    if (!$settings['enabled']) { return; }
    if (!($mailer instanceof VPS_Mail_Gateway_Mailer)) {
        global $phpmailer;
        $phpmailer = new VPS_Mail_Gateway_Mailer(true);
        $phpmailer->gateway_error = 'A conflicting plugin replaced the VPS Mail mailer.';
        return;
    }
    $mailer->gateway_error = '';
    if (!is_email($settings['from'])) {
        $mailer->gateway_error = 'VPS Mail requires a valid configured sender address.';
        return;
    }
    try {
        $mailer->setFrom($settings['from'], $settings['name'], false);
    } catch (\PHPMailer\PHPMailer\Exception $error) {
        $mailer->gateway_error = 'VPS Mail rejected the configured sender address.';
        return;
    }
    $mailer->Sender = $settings['from'];
    if ($settings['transport'] === 'api') {
        $mailer->Mailer = 'vpsmail';
    } elseif ($settings['transport'] === 'local') {
        $mailer->isSendmail();
        $mailer->Sendmail = '/usr/local/sbin/vps-sendmail';
    } else {
        $mailer->gateway_error = 'VPS Mail has an unknown transport.';
    }
}, PHP_INT_MAX);

add_action('admin_menu', function () {
    add_options_page('VPS Mail', 'VPS Mail', 'manage_options', 'vps-mail-gateway', 'vps_mail_settings_page');
});

add_action('admin_init', function () {
    register_setting('vps_mail_gateway', 'vps_mail_gateway', [
        'type' => 'array',
        'sanitize_callback' => function ($input) {
            $old = get_option('vps_mail_gateway', []);
            $old = is_array($old) ? $old : [];
            if (!is_array($input)) { return $old; }
            $from = sanitize_email($input['from'] ?? '');
            $endpoint = vps_mail_api_endpoint(trim((string) ($input['endpoint'] ?? '')));
            $transport = $input['transport'] ?? '';
            $key = trim((string) ($input['api_key'] ?? ''));
            $same_endpoint = $endpoint !== '' && $endpoint === vps_mail_api_endpoint($old['endpoint'] ?? '');
            $pinned = defined('VPS_MAIL_API_KEY') && defined('VPS_MAIL_ENDPOINT') && vps_mail_api_endpoint(VPS_MAIL_ENDPOINT) !== '';
            if (!$from || !in_array($transport, ['api', 'local'], true) ||
                ($transport === 'api' && !$pinned && (!$endpoint || (!$same_endpoint && $key === '')))) {
                add_settings_error('vps_mail_gateway', 'invalid', 'Enter a valid sender and an HTTPS endpoint for API transport.');
                return $old;
            }
            return [
                'enabled' => !empty($input['enabled']),
                'transport' => $transport,
                'endpoint' => $endpoint,
                'api_key' => $key !== '' ? $key : ($same_endpoint ? ($old['api_key'] ?? '') : ''),
                'from' => $from, 'name' => sanitize_text_field($input['name'] ?? ''),
            ];
        },
    ]);
});

function vps_mail_settings_page(): void {
    if (!current_user_can('manage_options')) { return; }
    $s = vps_mail_settings();
    ?>
    <div class="wrap"><h1>VPS Mail</h1>
    <p>WordPress email is accepted into the durable VPS Mail queue. Acceptance does not confirm inbox delivery.</p>
    <p>Use one mail transport plugin at a time. Local transport needs the VPS sendmail handler; API transport preserves MIME attachments up to 5 MiB per complete message.</p>
    <?php settings_errors('vps_mail_gateway'); ?>
    <form method="post" action="options.php">
    <?php settings_fields('vps_mail_gateway'); ?>
    <table class="form-table"><tbody>
    <tr><th>Enabled</th><td><input type="checkbox" name="vps_mail_gateway[enabled]" value="1" <?php checked($s['enabled']); ?>></td></tr>
    <tr><th><label for="vps-transport">Transport</label></th><td><select id="vps-transport" name="vps_mail_gateway[transport]">
    <option value="local" <?php selected($s['transport'], 'local'); ?>>Local sendmail handler</option>
    <option value="api" <?php selected($s['transport'], 'api'); ?>>REST API</option></select></td></tr>
    <tr><th><label for="vps-from">From address</label></th><td><input class="regular-text" type="email" id="vps-from" name="vps_mail_gateway[from]" value="<?php echo esc_attr($s['from']); ?>" required><p class="description">Use an enabled sender domain. This address overrides plugin-provided From addresses; Reply-To is preserved.</p></td></tr>
    <tr><th><label for="vps-name">From name</label></th><td><input class="regular-text" id="vps-name" name="vps_mail_gateway[name]" value="<?php echo esc_attr($s['name']); ?>"></td></tr>
    <tr><th><label for="vps-endpoint">API base URL</label></th><td><input class="regular-text" type="url" id="vps-endpoint" name="vps_mail_gateway[endpoint]" value="<?php echo esc_attr($s['endpoint']); ?>" placeholder="https://mail.example.com"></td></tr>
    <tr><th><label for="vps-key">API key</label></th><td><input class="regular-text" type="password" autocomplete="new-password" id="vps-key" name="vps_mail_gateway[api_key]" value=""><p class="description">Enter a new key when changing the endpoint. Configure VPS_MAIL_API_KEY together with VPS_MAIL_ENDPOINT in wp-config.php.</p></td></tr>
    </tbody></table>
    <?php submit_button(); ?>
    </form></div>
    <?php
}

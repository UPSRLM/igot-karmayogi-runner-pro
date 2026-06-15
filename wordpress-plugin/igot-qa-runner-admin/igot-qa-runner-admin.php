<?php
/**
 * Plugin Name: iGot QA Runner Admin
 * Description: Admin UI for submitting and monitoring iGot QA runner jobs against the hosted API.
 * Version: 0.1.0
 * Author: EchoNerve
 * Requires at least: 6.4
 * Requires PHP: 8.1
 * Text Domain: igot-qa-runner-admin
 */

declare(strict_types=1);

if (! defined('ABSPATH')) {
    exit;
}

final class IgotQaRunnerAdminPlugin {
    private const OPTION_SETTINGS = 'igot_qa_runner_admin_settings';
    private const OPTION_RECENT_RUNS = 'igot_qa_runner_admin_recent_runs';
    private const MENU_SLUG = 'igot-qa-runner-admin';
    private const NONCE_SETTINGS = 'igot_qa_runner_save_settings';
    private const NONCE_RUN = 'igot_qa_runner_submit_run';
    private const NONCE_DOWNLOAD = 'igot_qa_runner_download_artifact';

    public static function bootstrap(): void {
        $instance = new self();
        add_action('admin_menu', [$instance, 'register_menu']);
        add_action('admin_post_igot_qa_runner_save_settings', [$instance, 'handle_save_settings']);
        add_action('admin_post_igot_qa_runner_submit_run', [$instance, 'handle_submit_run']);
        add_action('admin_post_igot_qa_runner_download_artifact', [$instance, 'handle_download_artifact']);
    }

    public function register_menu(): void {
        add_menu_page(
            __('iGot QA Runner', 'igot-qa-runner-admin'),
            __('iGot QA Runner', 'igot-qa-runner-admin'),
            'manage_options',
            self::MENU_SLUG,
            [$this, 'render_admin_page'],
            'dashicons-controls-repeat',
            60
        );
    }

    public function render_admin_page(): void {
        if (! current_user_can('manage_options')) {
            wp_die(esc_html__('You do not have permission to do this.', 'igot-qa-runner-admin'));
        }

        $settings = $this->get_settings();
        $status_message = $this->get_flash_message('status_message');
        $error_message = $this->get_flash_message('error_message');
        $run_form = $this->get_flash_message('run_form');
        if (! is_array($run_form)) {
            $run_form = [];
        }
        $recent_runs = $this->load_recent_runs();
        $live_runs = $this->hydrate_runs($recent_runs, $settings);
        ?>
        <div class="wrap">
            <h1><?php echo esc_html__('iGot QA Runner', 'igot-qa-runner-admin'); ?></h1>
            <p><?php echo esc_html__('Use this page to submit runs to your hosted QA runner and download artifacts without exposing the API token in the browser.', 'igot-qa-runner-admin'); ?></p>

            <?php if ($status_message) : ?>
                <div class="notice notice-success is-dismissible"><p><?php echo esc_html($status_message); ?></p></div>
            <?php endif; ?>

            <?php if ($error_message) : ?>
                <div class="notice notice-error"><p><?php echo esc_html($error_message); ?></p></div>
            <?php endif; ?>

            <hr />
            <h2><?php echo esc_html__('Hosted API Settings', 'igot-qa-runner-admin'); ?></h2>
            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
                <input type="hidden" name="action" value="igot_qa_runner_save_settings" />
                <?php wp_nonce_field(self::NONCE_SETTINGS); ?>
                <table class="form-table" role="presentation">
                    <tr>
                        <th scope="row"><label for="igot_qa_api_base_url"><?php echo esc_html__('API Base URL', 'igot-qa-runner-admin'); ?></label></th>
                        <td>
                            <input type="url" class="regular-text" id="igot_qa_api_base_url" name="api_base_url" value="<?php echo esc_attr($settings['api_base_url']); ?>" placeholder="https://igot.echonerve.com" required />
                            <p class="description"><?php echo esc_html__('Base URL of the hosted FastAPI service, without a trailing slash.', 'igot-qa-runner-admin'); ?></p>
                        </td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_api_token"><?php echo esc_html__('API Bearer Token', 'igot-qa-runner-admin'); ?></label></th>
                        <td>
                            <input type="password" class="regular-text" id="igot_qa_api_token" name="api_token" value="" autocomplete="new-password" placeholder="<?php echo esc_attr($this->token_placeholder($settings['api_token'])); ?>" />
                            <p class="description"><?php echo esc_html__('Stored in WordPress options for authenticated server-to-server requests. Leave blank to keep the existing token.', 'igot-qa-runner-admin'); ?></p>
                        </td>
                    </tr>
                </table>
                <?php submit_button(__('Save Settings', 'igot-qa-runner-admin')); ?>
            </form>

            <hr />
            <h2><?php echo esc_html__('Submit New Run', 'igot-qa-runner-admin'); ?></h2>
            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>">
                <input type="hidden" name="action" value="igot_qa_runner_submit_run" />
                <?php wp_nonce_field(self::NONCE_RUN); ?>
                <table class="form-table" role="presentation">
                    <tr>
                        <th scope="row"><label for="igot_qa_start_url"><?php echo esc_html__('Start URL', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="url" class="regular-text" id="igot_qa_start_url" name="start_url" value="<?php echo esc_attr($run_form['start_url'] ?? ''); ?>" placeholder="https://portal.igotkarmayogi.gov.in/app/seeAll/new?key=continueLearning" /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_course_url"><?php echo esc_html__('Course URL', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="url" class="regular-text" id="igot_qa_course_url" name="course_url" value="<?php echo esc_attr($run_form['course_url'] ?? ''); ?>" placeholder="https://portal.igotkarmayogi.gov.in/..." /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_max_modules"><?php echo esc_html__('Max Modules', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="number" min="0" max="500" id="igot_qa_max_modules" name="max_modules" value="<?php echo esc_attr((string) ($run_form['max_modules'] ?? 50)); ?>" /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_loading_timeout_seconds"><?php echo esc_html__('Loading Timeout Seconds', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="number" min="5" max="300" id="igot_qa_loading_timeout_seconds" name="loading_timeout_seconds" value="<?php echo esc_attr((string) ($run_form['loading_timeout_seconds'] ?? 35)); ?>" /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_video_speed"><?php echo esc_html__('Video Speed', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="number" step="0.5" min="0.5" max="16" id="igot_qa_video_speed" name="video_speed" value="<?php echo esc_attr((string) ($run_form['video_speed'] ?? 2.0)); ?>" /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_video_max_wait_seconds"><?php echo esc_html__('Video Max Wait Seconds', 'igot-qa-runner-admin'); ?></label></th>
                        <td><input type="number" min="30" max="14400" id="igot_qa_video_max_wait_seconds" name="video_max_wait_seconds" value="<?php echo esc_attr((string) ($run_form['video_max_wait_seconds'] ?? 2400)); ?>" /></td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_groq_api_key"><?php echo esc_html__('Groq API Key', 'igot-qa-runner-admin'); ?></label></th>
                        <td>
                            <input type="password" class="regular-text" id="igot_qa_groq_api_key" name="groq_api_key" value="" autocomplete="off" />
                            <p class="description"><?php echo esc_html__('Optional. Sent only for this run and not stored in WordPress.', 'igot-qa-runner-admin'); ?></p>
                        </td>
                    </tr>
                    <tr>
                        <th scope="row"><label for="igot_qa_gemini_api_key"><?php echo esc_html__('Gemini API Key', 'igot-qa-runner-admin'); ?></label></th>
                        <td>
                            <input type="password" class="regular-text" id="igot_qa_gemini_api_key" name="gemini_api_key" value="" autocomplete="off" />
                            <p class="description"><?php echo esc_html__('Optional. Sent only for this run and not stored in WordPress.', 'igot-qa-runner-admin'); ?></p>
                        </td>
                    </tr>
                </table>
                <fieldset>
                    <legend class="screen-reader-text"><?php echo esc_html__('Run Options', 'igot-qa-runner-admin'); ?></legend>
                    <label><input type="checkbox" name="strict_sequence" value="1" <?php checked($this->as_bool($run_form['strict_sequence'] ?? true)); ?> /> <?php echo esc_html__('Strict Sequence', 'igot-qa-runner-admin'); ?></label><br />
                    <label><input type="checkbox" name="auto_run_to_end" value="1" <?php checked($this->as_bool($run_form['auto_run_to_end'] ?? true)); ?> /> <?php echo esc_html__('Auto Run To End', 'igot-qa-runner-admin'); ?></label><br />
                    <label><input type="checkbox" name="skip_assessments" value="1" <?php checked($this->as_bool($run_form['skip_assessments'] ?? false)); ?> /> <?php echo esc_html__('Skip Assessments', 'igot-qa-runner-admin'); ?></label><br />
                    <label><input type="checkbox" name="pause_for_quiz" value="1" <?php checked($this->as_bool($run_form['pause_for_quiz'] ?? false)); ?> /> <?php echo esc_html__('Pause For Quiz', 'igot-qa-runner-admin'); ?></label><br />
                    <label><input type="checkbox" name="continue_on_error" value="1" <?php checked($this->as_bool($run_form['continue_on_error'] ?? true)); ?> /> <?php echo esc_html__('Continue On Error', 'igot-qa-runner-admin'); ?></label><br />
                    <label><input type="checkbox" name="headless" value="1" <?php checked($this->as_bool($run_form['headless'] ?? true)); ?> /> <?php echo esc_html__('Headless', 'igot-qa-runner-admin'); ?></label>
                </fieldset>
                <?php submit_button(__('Submit Run', 'igot-qa-runner-admin'), 'primary', 'submit_run'); ?>
            </form>

            <hr />
            <h2><?php echo esc_html__('Recent Runs', 'igot-qa-runner-admin'); ?></h2>
            <?php if (empty($live_runs)) : ?>
                <p><?php echo esc_html__('No runs submitted from this plugin yet.', 'igot-qa-runner-admin'); ?></p>
            <?php else : ?>
                <table class="widefat striped">
                    <thead>
                        <tr>
                            <th><?php echo esc_html__('Run ID', 'igot-qa-runner-admin'); ?></th>
                            <th><?php echo esc_html__('Status', 'igot-qa-runner-admin'); ?></th>
                            <th><?php echo esc_html__('Queued At', 'igot-qa-runner-admin'); ?></th>
                            <th><?php echo esc_html__('Finished At', 'igot-qa-runner-admin'); ?></th>
                            <th><?php echo esc_html__('Error', 'igot-qa-runner-admin'); ?></th>
                            <th><?php echo esc_html__('Artifacts', 'igot-qa-runner-admin'); ?></th>
                        </tr>
                    </thead>
                    <tbody>
                        <?php foreach ($live_runs as $run) : ?>
                            <tr>
                                <td><code><?php echo esc_html((string) ($run['run_id'] ?? '')); ?></code></td>
                                <td><?php echo esc_html((string) ($run['status'] ?? 'unknown')); ?></td>
                                <td><?php echo esc_html((string) ($run['queued_at'] ?? '')); ?></td>
                                <td><?php echo esc_html((string) ($run['finished_at'] ?? '')); ?></td>
                                <td><?php echo esc_html((string) ($run['error_message'] ?? '')); ?></td>
                                <td>
                                    <?php if (! empty($run['artifacts']) && is_array($run['artifacts'])) : ?>
                                        <ul>
                                            <?php foreach ($run['artifacts'] as $artifact) : ?>
                                                <?php $download_url = $this->build_download_url((string) ($run['run_id'] ?? ''), (string) ($artifact['relative_path'] ?? '')); ?>
                                                <li><a href="<?php echo esc_url($download_url); ?>"><?php echo esc_html((string) ($artifact['relative_path'] ?? 'artifact')); ?></a></li>
                                            <?php endforeach; ?>
                                        </ul>
                                    <?php else : ?>
                                        <?php echo esc_html__('No artifacts yet', 'igot-qa-runner-admin'); ?>
                                    <?php endif; ?>
                                </td>
                            </tr>
                        <?php endforeach; ?>
                    </tbody>
                </table>
            <?php endif; ?>
        </div>
        <?php
    }

    public function handle_save_settings(): void {
        $this->enforce_admin();
        check_admin_referer(self::NONCE_SETTINGS);

        $api_base_url = esc_url_raw(wp_unslash($_POST['api_base_url'] ?? ''));
        $api_token = sanitize_text_field(wp_unslash($_POST['api_token'] ?? ''));

        if ('' === $api_base_url) {
            $this->set_flash_message('error_message', __('API Base URL is required.', 'igot-qa-runner-admin'));
            $this->redirect_to_admin_page();
        }

        if ('' === $api_token) {
            $existing_settings = $this->get_settings();
            $api_token = (string) ($existing_settings['api_token'] ?? '');
        }

        if ('' === $api_token) {
            $this->set_flash_message('error_message', __('API token is required.', 'igot-qa-runner-admin'));
            $this->redirect_to_admin_page();
        }

        update_option(
            self::OPTION_SETTINGS,
            [
                'api_base_url' => untrailingslashit($api_base_url),
                'api_token' => $api_token,
            ],
            false
        );

        $this->set_flash_message('status_message', __('Settings saved.', 'igot-qa-runner-admin'));
        $this->redirect_to_admin_page();
    }

    public function handle_submit_run(): void {
        $this->enforce_admin();
        check_admin_referer(self::NONCE_RUN);

        $settings = $this->get_settings();
        if ('' === $settings['api_base_url'] || '' === $settings['api_token']) {
            $this->set_flash_message('error_message', __('Configure the hosted API settings first.', 'igot-qa-runner-admin'));
            $this->redirect_to_admin_page();
        }

        $payload = $this->build_run_payload();
        $this->set_flash_message('run_form', $this->payload_for_flash($payload));

        if (empty($payload['start_url']) && empty($payload['course_url'])) {
            $this->set_flash_message('error_message', __('Provide either a Start URL or a Course URL.', 'igot-qa-runner-admin'));
            $this->redirect_to_admin_page();
        }

        $response = $this->api_request($settings, 'POST', '/api/runs', $payload);
        if (is_wp_error($response)) {
            $this->set_flash_message('error_message', $response->get_error_message());
            $this->redirect_to_admin_page();
        }

        $status_code = (int) wp_remote_retrieve_response_code($response);
        $body = json_decode((string) wp_remote_retrieve_body($response), true);
        if (202 !== $status_code || ! is_array($body) || empty($body['run_id'])) {
            $message = is_array($body) && ! empty($body['detail']) ? (string) $body['detail'] : __('The hosted API rejected the run request.', 'igot-qa-runner-admin');
            $this->set_flash_message('error_message', $message);
            $this->redirect_to_admin_page();
        }

        $this->remember_run([
            'run_id' => sanitize_text_field((string) $body['run_id']),
            'status' => sanitize_text_field((string) ($body['status'] ?? 'queued')),
            'queued_at' => sanitize_text_field((string) ($body['queued_at'] ?? '')),
        ]);

        delete_transient($this->flash_key('run_form'));
        $this->set_flash_message('status_message', sprintf(__('Run %s was submitted successfully.', 'igot-qa-runner-admin'), (string) $body['run_id']));
        $this->redirect_to_admin_page();
    }

    public function handle_download_artifact(): void {
        $this->enforce_admin();

        $nonce = sanitize_text_field(wp_unslash($_GET['_wpnonce'] ?? ''));
        if (! wp_verify_nonce($nonce, self::NONCE_DOWNLOAD)) {
            wp_die(esc_html__('Security check failed.', 'igot-qa-runner-admin'));
        }

        $run_id = sanitize_text_field(wp_unslash($_GET['run_id'] ?? ''));
        $artifact_path = sanitize_text_field(wp_unslash($_GET['artifact_path'] ?? ''));
        if ('' === $run_id || '' === $artifact_path) {
            wp_die(esc_html__('Missing run or artifact.', 'igot-qa-runner-admin'));
        }

        $settings = $this->get_settings();
        $encoded_parts = array_map('rawurlencode', explode('/', str_replace('\\', '/', $artifact_path)));
        $path = '/api/runs/' . rawurlencode($run_id) . '/artifacts/' . implode('/', $encoded_parts);
        $response = $this->api_request($settings, 'GET', $path, null, 120);

        if (is_wp_error($response)) {
            wp_die(esc_html($response->get_error_message()));
        }

        $status_code = (int) wp_remote_retrieve_response_code($response);
        if (200 !== $status_code) {
            wp_die(esc_html__('Unable to download the artifact from the hosted API.', 'igot-qa-runner-admin'));
        }

        $body = wp_remote_retrieve_body($response);
        $filename = basename($artifact_path);
        header('Content-Description: File Transfer');
        header('Content-Type: application/octet-stream');
        header('Content-Disposition: attachment; filename="' . sanitize_file_name($filename) . '"');
        header('Content-Length: ' . strlen((string) $body));
        echo $body; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped
        exit;
    }

    private function build_run_payload(): array {
        $payload = [
            'start_url' => esc_url_raw(wp_unslash($_POST['start_url'] ?? '')),
            'course_url' => esc_url_raw(wp_unslash($_POST['course_url'] ?? '')),
            'max_modules' => $this->sanitize_int($_POST['max_modules'] ?? 50, 0, 500, 50),
            'loading_timeout_seconds' => $this->sanitize_int($_POST['loading_timeout_seconds'] ?? 35, 5, 300, 35),
            'video_speed' => $this->sanitize_float($_POST['video_speed'] ?? 2.0, 0.5, 16.0, 2.0),
            'video_max_wait_seconds' => $this->sanitize_int($_POST['video_max_wait_seconds'] ?? 2400, 30, 14400, 2400),
            'strict_sequence' => ! empty($_POST['strict_sequence']),
            'auto_run_to_end' => ! empty($_POST['auto_run_to_end']),
            'skip_assessments' => ! empty($_POST['skip_assessments']),
            'pause_for_quiz' => ! empty($_POST['pause_for_quiz']),
            'continue_on_error' => ! empty($_POST['continue_on_error']),
            'headless' => ! empty($_POST['headless']),
        ];

        $groq_api_key = sanitize_text_field(wp_unslash($_POST['groq_api_key'] ?? ''));
        $gemini_api_key = sanitize_text_field(wp_unslash($_POST['gemini_api_key'] ?? ''));

        if ('' !== $groq_api_key) {
            $payload['groq_api_key'] = $groq_api_key;
        }

        if ('' !== $gemini_api_key) {
            $payload['gemini_api_key'] = $gemini_api_key;
        }

        return $payload;
    }

    private function api_request(array $settings, string $method, string $path, ?array $body = null, int $timeout = 60) {
        $args = [
            'method' => $method,
            'timeout' => $timeout,
            'headers' => [
                'Authorization' => 'Bearer ' . $settings['api_token'],
                'Accept' => 'application/json',
            ],
        ];

        if (null !== $body) {
            $args['headers']['Content-Type'] = 'application/json';
            $args['body'] = wp_json_encode($body);
        }

        return wp_remote_request(untrailingslashit($settings['api_base_url']) . $path, $args);
    }

    private function hydrate_runs(array $recent_runs, array $settings): array {
        if ('' === $settings['api_base_url'] || '' === $settings['api_token']) {
            return $recent_runs;
        }

        $hydrated = [];
        foreach ($recent_runs as $run) {
            $run_id = sanitize_text_field((string) ($run['run_id'] ?? ''));
            if ('' === $run_id) {
                continue;
            }
            $response = $this->api_request($settings, 'GET', '/api/runs/' . rawurlencode($run_id), null, 30);
            if (is_wp_error($response)) {
                $run['error_message'] = $response->get_error_message();
                $hydrated[] = $run;
                continue;
            }
            $status_code = (int) wp_remote_retrieve_response_code($response);
            if (200 !== $status_code) {
                $body = json_decode((string) wp_remote_retrieve_body($response), true);
                $run['error_message'] = is_array($body) && ! empty($body['detail'])
                    ? (string) $body['detail']
                    : __('Unable to fetch run status from the hosted API.', 'igot-qa-runner-admin');
                $hydrated[] = $run;
                continue;
            }
            $body = json_decode((string) wp_remote_retrieve_body($response), true);
            if (is_array($body)) {
                $hydrated[] = $body;
            } else {
                $run['error_message'] = __('Unable to decode run status response.', 'igot-qa-runner-admin');
                $hydrated[] = $run;
            }
        }

        return $hydrated;
    }

    private function remember_run(array $run): void {
        $recent_runs = $this->load_recent_runs();
        array_unshift($recent_runs, $run);
        $recent_runs = array_slice($recent_runs, 0, 20);
        update_option(self::OPTION_RECENT_RUNS, $recent_runs, false);
    }

    private function load_recent_runs(): array {
        $runs = get_option(self::OPTION_RECENT_RUNS, []);
        return is_array($runs) ? $runs : [];
    }

    private function get_settings(): array {
        $defaults = [
            'api_base_url' => '',
            'api_token' => '',
        ];
        $settings = get_option(self::OPTION_SETTINGS, []);
        return wp_parse_args(is_array($settings) ? $settings : [], $defaults);
    }

    private function build_download_url(string $run_id, string $artifact_path): string {
        return wp_nonce_url(
            add_query_arg(
                [
                    'action' => 'igot_qa_runner_download_artifact',
                    'run_id' => $run_id,
                    'artifact_path' => $artifact_path,
                ],
                admin_url('admin-post.php')
            ),
            self::NONCE_DOWNLOAD
        );
    }

    private function flash_key(string $key): string {
        return 'igot_qa_runner_flash_' . $key . '_' . get_current_user_id();
    }

    private function set_flash_message(string $key, $value): void {
        set_transient($this->flash_key($key), $value, MINUTE_IN_SECONDS * 10);
    }

    private function get_flash_message(string $key) {
        $value = get_transient($this->flash_key($key));
        delete_transient($this->flash_key($key));
        return $value;
    }

    private function enforce_admin(): void {
        if (! current_user_can('manage_options')) {
            wp_die(esc_html__('You do not have permission to do this.', 'igot-qa-runner-admin'));
        }
    }

    private function redirect_to_admin_page(): void {
        wp_safe_redirect(admin_url('admin.php?page=' . self::MENU_SLUG));
        exit;
    }

    private function sanitize_int($value, int $min, int $max, int $default): int {
        $sanitized = filter_var($value, FILTER_VALIDATE_INT);
        if (false === $sanitized) {
            return $default;
        }
        return max($min, min($max, (int) $sanitized));
    }

    private function sanitize_float($value, float $min, float $max, float $default): float {
        $sanitized = filter_var($value, FILTER_VALIDATE_FLOAT);
        if (false === $sanitized) {
            return $default;
        }
        return max($min, min($max, (float) $sanitized));
    }

    private function as_bool($value): bool {
        return in_array($value, [true, '1', 1, 'true', 'on'], true);
    }

    private function payload_for_flash(array $payload): array {
        unset($payload['groq_api_key'], $payload['gemini_api_key']);
        return $payload;
    }

    private function token_placeholder(string $token): string {
        if ('' === $token) {
            return '';
        }

        $length = strlen($token);
        if ($length <= 8) {
            return str_repeat('*', $length);
        }

        return substr($token, 0, 4) . str_repeat('*', max(0, $length - 8)) . substr($token, -4);
    }
}

IgotQaRunnerAdminPlugin::bootstrap();

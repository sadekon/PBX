/**
 * Configuration page module.
 * Handles system configuration, feature toggles, and SSL management.
 */

import { fetchWithTimeout, getAuthHeaders, getApiBaseUrl } from '../api/client.ts';
import { showNotification } from '../ui/notifications.ts';
import { escapeHtml } from '../utils/html.ts';
import { withButtonGuard } from '../utils/debounce.ts';

interface VoicemailConfig {
    max_duration?: number;
    max_messages?: number;
}

interface FullConfig {
    features?: Record<string, boolean>;
    voicemail?: VoicemailConfig;
}

interface FeatureEntry {
    enabled: boolean;
    description: string;
}

interface FeaturesResponse {
    features?: Record<string, boolean>;
    core?: Record<string, FeatureEntry>;
    advanced?: Record<string, FeatureEntry>;
    integrations?: Record<string, FeatureEntry>;
}

interface SSLCertificate {
    subject?: string;
    issuer?: string;
    expires?: string;
}

interface SSLStatus {
    enabled: boolean;
    certificate?: SSLCertificate;
}

interface ErrorResponse {
    error?: string;
}

const CONFIG_SAVE_SUCCESS_MESSAGE = 'Configuration saved successfully. Restart may be required for some changes.';

export async function loadConfig(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/config/full`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const config: FullConfig = await response.json();

        // Feature Toggles
        if (config.features) {
            const featureIds = [
                'call-recording', 'call-transfer', 'call-hold', 'conference',
                'voicemail', 'call-parking', 'call-queues', 'presence',
                'music-on-hold', 'auto-attendant',
            ] as const;
            for (const id of featureIds) {
                const el = document.getElementById(`feature-${id}`) as HTMLInputElement | null;
                const key = id.replace(/-/g, '_');
                if (el) el.checked = config.features[key] ?? false;
            }
        }

        // Populate other config sections
        if (config.voicemail) {
            const el = (id: string): HTMLElement | null => document.getElementById(id);
            if (el('voicemail-max-duration')) (el('voicemail-max-duration') as HTMLInputElement).value = String(config.voicemail.max_duration ?? 120);
        }
    } catch (error: unknown) {
        console.error('Error loading config:', error);
        showNotification('Failed to load configuration', 'error');
    }
}

export async function loadFeaturesStatus(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/config/features`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: FeaturesResponse = await response.json();

        renderFeatureTable('core-features-table', data.core);
        renderFeatureTable('advanced-features-table', data.advanced);
        renderFeatureTable('integration-features-table', data.integrations);
    } catch (error: unknown) {
        console.error('Error loading features status:', error);
        showNotification('Failed to load feature status', 'error');
    }
}

function renderFeatureTable(
    elementId: string,
    features: Record<string, FeatureEntry> | undefined,
): void {
    const tbody = document.getElementById(elementId) as HTMLElement | null;
    if (!tbody) return;

    if (!features || Object.keys(features).length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;">No features found</td></tr>';
        return;
    }

    tbody.innerHTML = Object.entries(features).map(([key, feature]) => {
        const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        const badge = feature.enabled
            ? '<span class="badge" style="background:#10b981;">Enabled</span>'
            : '<span class="badge" style="background:#6b7280;">Disabled</span>';
        return `<tr>
            <td><strong>${escapeHtml(label)}</strong></td>
            <td>${badge}</td>
            <td>${escapeHtml(feature.description)}</td>
        </tr>`;
    }).join('');
}

export async function saveConfigSection(section: string): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const form = document.getElementById(`${section}-form`) as HTMLFormElement | null;
        if (!form) return;

        const formData = new FormData(form);
        const data = Object.fromEntries(formData.entries());

        const response = await fetch(`${API_BASE}/api/config/section`, {
            method: 'PUT',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ section, data })
        });

        if (response.ok) {
            showNotification(CONFIG_SAVE_SUCCESS_MESSAGE, 'success');
        } else {
            const error: ErrorResponse = await response.json();
            showNotification(error.error || 'Failed to save configuration', 'error');
        }
    } catch (error: unknown) {
        console.error(`Error saving ${section} config:`, error);
        showNotification(`Failed to save ${section} configuration`, 'error');
    }
}

export async function loadSSLStatus(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/ssl/status`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data: SSLStatus = await response.json();

        const statusEl = document.getElementById('ssl-status-info') as HTMLElement | null;
        if (statusEl) {
            statusEl.textContent = data.enabled ? 'Enabled' : 'Disabled';
            statusEl.className = `status-badge ${data.enabled ? 'enabled' : 'disabled'}`;
        }

        if (data.certificate) {
            const certSection = document.getElementById('cert-details-section') as HTMLElement | null;
            if (certSection) certSection.style.display = 'block';

            const subjectEl = document.getElementById('cert-subject') as HTMLInputElement | null;
            const issuerEl = document.getElementById('cert-issuer') as HTMLInputElement | null;
            const validFromEl = document.getElementById('cert-valid-from') as HTMLInputElement | null;
            const validUntilEl = document.getElementById('cert-valid-until') as HTMLInputElement | null;
            if (subjectEl) subjectEl.value = data.certificate.subject || 'N/A';
            if (issuerEl) issuerEl.value = data.certificate.issuer || 'N/A';
            if (validFromEl) validFromEl.value = data.certificate.expires || 'N/A';
            if (validUntilEl) validUntilEl.value = data.certificate.expires || 'N/A';
        }
    } catch (error: unknown) {
        console.error('Error loading SSL status:', error);
    }
}

export async function generateSSLCertificate(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetch(`${API_BASE}/api/ssl/generate-certificate`, {
            method: 'POST',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({})
        });

        if (response.ok) {
            showNotification('SSL certificate generated successfully. Server restart required.', 'success');
            loadSSLStatus();
        } else {
            const error: ErrorResponse = await response.json();
            showNotification(error.error || 'Failed to generate SSL certificate', 'error');
        }
    } catch (error: unknown) {
        console.error('Error generating SSL certificate:', error);
        showNotification('Failed to generate SSL certificate', 'error');
    }
}

export async function refreshSSLStatus(): Promise<void> {
    await loadSSLStatus();
    showNotification('SSL status refreshed', 'success');
}

const CONFIG_FORM_SECTIONS = [
    'features-config',
    'voicemail-config',
    'email-config',
    'recording-config',
    'security-config',
    'advanced-features',
    'conference-config',
    'ssl-config',
] as const;


interface SmtpConfig {
    host?: string;
    port?: number;
    security?: string;
    auth?: string;
    username?: string;
    from_address?: string;
    from_name?: string;
    ca_file?: string;
    helo_hostname?: string;
    verify_cert?: boolean;
    password?: string;
}

/**
 * Populate the Email/SMTP form from the saved configuration.
 *
 * Values come from the server already resolved -- ${VAR} substitution applied, defaults
 * filled in, password masked -- so what is shown is what the mailer is actually using.
 */
export async function loadEmailConfig(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const response = await fetchWithTimeout(`${API_BASE}/api/config`, {
            headers: getAuthHeaders()
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const config: { smtp?: SmtpConfig; smtp_warnings?: string[] } = await response.json();
        const smtp = config.smtp ?? {};
        const set = (id: string, value: string): void => {
            const el = document.getElementById(id) as HTMLInputElement | HTMLSelectElement | null;
            if (el) el.value = value;
        };

        set('smtp-host', smtp.host ?? '');
        set('smtp-port', String(smtp.port ?? 587));
        set('smtp-security', smtp.security ?? 'starttls');
        set('smtp-auth', smtp.auth ?? 'none');
        set('smtp-username', smtp.username ?? '');
        set('email-from', smtp.from_address ?? '');
        set('email-from-name', smtp.from_name ?? '');
        set('smtp-ca-file', smtp.ca_file ?? '');
        set('smtp-helo', smtp.helo_hostname ?? '');

        const verify = document.getElementById('smtp-verify-cert') as HTMLInputElement | null;
        if (verify) verify.checked = smtp.verify_cert !== false;

        // The password is environment-managed; show only whether one is present.
        set('smtp-password-status', smtp.password ? 'Set via SMTP_PASSWORD' : 'Not set');

        const warnings = document.getElementById('smtp-config-warnings') as HTMLElement | null;
        if (warnings) {
            const problems = config.smtp_warnings ?? [];
            warnings.style.display = problems.length ? 'block' : 'none';
            warnings.innerHTML = problems.length
                ? `<strong>Configuration warnings:</strong><ul>${
                    problems.map((p) => `<li>${escapeHtml(p)}</li>`).join('')
                  }</ul>`
                : '';
        }
    } catch (error: unknown) {
        console.error('Error loading email config:', error);
        showNotification('Failed to load email configuration', 'error');
    }
}

/**
 * Persist the Email/SMTP form to config.yml.
 *
 * The password field is deliberately absent: it is read from SMTP_PASSWORD, and submitting
 * one here would write a plaintext secret into a tracked file.
 */
export async function saveEmailConfig(): Promise<void> {
    try {
        const API_BASE = getApiBaseUrl();
        const form = document.getElementById('email-config-form') as HTMLFormElement | null;
        if (!form) return;

        const formData = new FormData(form);
        const smtp: Record<string, unknown> = Object.fromEntries(formData.entries());
        // An unchecked checkbox is simply absent from FormData, so derive it explicitly.
        smtp.verify_cert =
            (document.getElementById('smtp-verify-cert') as HTMLInputElement | null)?.checked ?? true;

        const response = await fetch(`${API_BASE}/api/config`, {
            method: 'PUT',
            headers: { ...getAuthHeaders(), 'Content-Type': 'application/json' },
            body: JSON.stringify({ smtp })
        });

        if (response.ok) {
            showNotification('Email settings saved. Restart required to take effect.', 'success');
            await loadEmailConfig();
        } else {
            const error: ErrorResponse = await response.json();
            showNotification(error.error || 'Failed to save email settings', 'error');
        }
    } catch (error: unknown) {
        console.error('Error saving email config:', error);
        showNotification('Failed to save email settings', 'error');
    }
}

function initConfigForms(): void {
    // The Email/SMTP form is not part of CONFIG_FORM_SECTIONS: it posts the whole smtp
    // section to /api/config rather than a named section, and needs its own load step.
    const emailForm = document.getElementById('email-config-form') as HTMLFormElement | null;
    if (emailForm) {
        emailForm.addEventListener('submit', async (e: Event) => {
            e.preventDefault();
            await saveEmailConfig();
        });
    }

    for (const section of CONFIG_FORM_SECTIONS) {
        const form = document.getElementById(`${section}-form`) as HTMLFormElement | null;
        if (form) {
            form.addEventListener('submit', async (e: Event) => {
                e.preventDefault();
                await saveConfigSection(section);
            });
        }
    }
}

// Backward compatibility
window.loadConfig = loadConfig;
window.loadEmailConfig = loadEmailConfig;
window.saveEmailConfig = saveEmailConfig;
window.loadFeaturesStatus = loadFeaturesStatus;
window.saveConfigSection = saveConfigSection;
window.loadSSLStatus = loadSSLStatus;
window.generateSSLCertificate = generateSSLCertificate;
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- legacy backward compat
(window as any).refreshSSLStatus = refreshSSLStatus;

// Self-initialize form handlers once the DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initConfigForms);
} else {
    initConfigForms();
}

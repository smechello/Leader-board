import os

def patch_file(path, func):
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    new_content = func(content)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f"Patched {path}")

def update_base(content):
    if 'id="theme-toggler"' not in content:
        nav_insertion = """                <li class="nav-item ms-lg-3 mt-2 mt-lg-0 d-flex align-items-center">
                    <button class="btn btn-link nav-link px-2" id="theme-toggler" title="Toggle Theme">
                        <i class="bi bi-moon-stars-fill" id="theme-icon"></i>
                    </button>
                </li>
                <li class="nav-item">
                    <button class="btn btn-link nav-link px-2" type="button" data-bs-toggle="offcanvas" data-bs-target="#judgeGuideOffcanvas" aria-controls="judgeGuideOffcanvas" title="User Guide">
                        <i class="bi bi-info-circle-fill"></i>
                    </button>
                </li>"""
        content = content.replace('<ul class="navbar-nav ms-auto align-items-lg-center">', '<ul class="navbar-nav ms-auto align-items-lg-center">\n' + nav_insertion)
        
        script_insertion = """<script>
(() => {
    const getStoredTheme = () => localStorage.getItem('theme') || 'light';
    const setStoredTheme = theme => localStorage.setItem('theme', theme);
    const setTheme = theme => {
        document.documentElement.setAttribute('data-bs-theme', theme);
        const icon = document.getElementById('theme-icon');
        if(icon) {
            icon.className = theme === 'dark' ? 'bi bi-sun-fill' : 'bi bi-moon-stars-fill';
        }
    };
    setTheme(getStoredTheme());
    window.addEventListener('DOMContentLoaded', () => {
        const toggler = document.getElementById('theme-toggler');
        if(toggler) {
            toggler.addEventListener('click', () => {
                const current = document.documentElement.getAttribute('data-bs-theme');
                const nextTheme = current === 'dark' ? 'light' : 'dark';
                setStoredTheme(nextTheme);
                setTheme(nextTheme);
            });
        }
    });
})();
</script>"""
        content = content.replace('</head>', script_insertion + '\n</head>')

        offcanvas_insertion = """<!-- Judge Guide Offcanvas -->
<div class="offcanvas offcanvas-end" tabindex="-1" id="judgeGuideOffcanvas" aria-labelledby="judgeGuideOffcanvasLabel">
  <div class="offcanvas-header bg-dark text-white">
    <h5 class="offcanvas-title" id="judgeGuideOffcanvasLabel"><i class="bi bi-book me-2"></i>Judge User Guide</h5>
    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="offcanvas" aria-label="Close"></button>
  </div>
  <div class="offcanvas-body">
    <h6>Welcome to the Evaluation Console!</h6>
    <p class="text-secondary small">This dashboard is designed to help you quickly and accurately score teams.</p>
    
    <div class="mb-4">
        <h6><i class="bi bi-speedometer2 text-primary me-2"></i>Dashboard Overview</h6>
        <ul class="small text-secondary">
            <li><strong>Total Teams:</strong> The number of teams assigned to you.</li>
            <li><strong>Completed:</strong> Teams you have fully scored.</li>
            <li><strong>Locked:</strong> Scores you have finalized and cannot change.</li>
        </ul>
    </div>
    
    <div class="mb-4">
        <h6><i class="bi bi-star-fill text-warning me-2"></i>Scoring Teams</h6>
        <ul class="small text-secondary">
            <li>Click <strong class="text-primary">Score Team</strong> to begin evaluating a team.</li>
            <li>Scores calculate dynamically based on category weights.</li>
            <li><strong>Save Scores:</strong> Saves your current progress.</li>
            <li><strong>Save and Lock:</strong> Finalizes the score (cannot be edited later).</li>
        </ul>
    </div>
    
    <div class="mb-4">
        <h6><i class="bi bi-bell-fill text-danger me-2"></i>Live Tracking</h6>
        <p class="small text-secondary">The system automatically tracks the presentation state. You will see prompts to move to the Next Team automatically when the session timer advances.</p>
    </div>
  </div>
</div>"""
        content = content.replace('</main>', '</main>\n' + offcanvas_insertion)
    return content

def update_dashboard(content):
    if '[data-bs-theme="dark"]' not in content:
        dark_css = """
[data-bs-theme="dark"] .judge-dash-shell { --ink: #e2e8f0; --ink-soft: #94a3b8; --surface: #1e293b; --line: rgba(255, 255, 255, 0.1); }
[data-bs-theme="dark"] .judge-dash-hero { background: linear-gradient(130deg, #1e293b, #0f172a); border-color: rgba(255, 255, 255, 0.1); box-shadow: 0 14px 32px rgba(0,0,0, 0.4); color: var(--ink); }
[data-bs-theme="dark"] .judge-metric-card { background: #0f172a; box-shadow: none; border-color: rgba(255, 255, 255, 0.1); color: var(--ink); }
[data-bs-theme="dark"] .judge-focus-card { background: linear-gradient(90deg, rgba(6, 123, 194, 0.15), rgba(0, 166, 166, 0.15)); border-color: rgba(6, 123, 194, 0.3); color: var(--ink); }
[data-bs-theme="dark"] .judge-focus-chip { background: rgba(15, 23, 42, 0.8); border-color: rgba(255, 255, 255, 0.1); color: var(--ink); }
[data-bs-theme="dark"] .judge-table-card { background: #1e293b; border-color: rgba(255, 255, 255, 0.1); }
[data-bs-theme="dark"] .judge-table { color: var(--ink); }
[data-bs-theme="dark"] .text-dark { color: var(--ink) !important; }
[data-bs-theme="dark"] .card { background: #1e293b; }
"""
        content = content.replace('</style>', dark_css + '\n</style>')
    return content

def update_score_team(content):
    if '[data-bs-theme="dark"]' not in content:
        dark_css = """
[data-bs-theme="dark"] .judge-score-shell { --ink: #e2e8f0; --ink-soft: #94a3b8; --surface: #1e293b; --line: rgba(255, 255, 255, 0.1); }
[data-bs-theme="dark"] .judge-score-hero { background: linear-gradient(115deg, #1e293b, #0f172a); border-color: var(--line); color: var(--ink); }
[data-bs-theme="dark"] .judge-meta-card { background: #0f172a; border-color: var(--line); color: var(--ink); }
[data-bs-theme="dark"] .judge-form-card { background: #1e293b; border-color: var(--line); color: var(--ink); }
[data-bs-theme="dark"] .judge-category-card { background: #0f172a; border-color: var(--line); color: var(--ink); }
[data-bs-theme="dark"] .judge-score-input, [data-bs-theme="dark"] .judge-remarks-input { background: #1e293b; border-color: var(--line); color: #e2e8f0; }
[data-bs-theme="dark"] .judge-score-input:focus, [data-bs-theme="dark"] .judge-remarks-input:focus { background: #0f172a; border-color: var(--brand); color: #e2e8f0; }
[data-bs-theme="dark"] .judge-total-panel { background: #0f172a; border-color: var(--line); color: var(--ink); }
[data-bs-theme="dark"] .card-footer { background: #1e293b !important; border-top: 1px solid var(--line) !important; }
[data-bs-theme="dark"] .judge-form-card .card-body { background: #1e293b; }
"""
        content = content.replace('</style>', dark_css + '\n</style>')
    return content


base_path = 'Leader-board/templates/base.html'
dash_path = 'Leader-board/templates/judge/dashboard.html'
score_path = 'Leader-board/templates/judge/score_team.html'

patch_file(base_path, update_base)
patch_file(dash_path, update_dashboard)
patch_file(score_path, update_score_team)

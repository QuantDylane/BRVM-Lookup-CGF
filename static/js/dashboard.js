/* ============================================
   LOOK UP BRVM - Dashboard JavaScript
   ============================================ */

// Sidebar Toggle
document.addEventListener('DOMContentLoaded', function() {
    const sidebar = document.getElementById('sidebar');
    const toggle = document.getElementById('sidebarToggle');

    if (toggle && sidebar) {
        toggle.addEventListener('click', function() {
            sidebar.classList.toggle('collapsed');
            sidebar.classList.toggle('show');
        });
    }

    // Init sortable tables
    initSortableTables();
});

// ---- Sortable Tables ----
function initSortableTables() {
    document.querySelectorAll('table.sortable').forEach(function(table) {
        const headers = table.querySelectorAll('thead th[data-sort]');
        headers.forEach(function(th, index) {
            th.addEventListener('click', function() {
                sortTable(table, index, th);
            });
        });
    });
}

function sortTable(table, colIndex, th) {
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const isAsc = th.classList.contains('sort-asc');

    // Reset all headers
    table.querySelectorAll('thead th').forEach(h => {
        h.classList.remove('sort-asc', 'sort-desc');
    });

    const direction = isAsc ? -1 : 1;
    th.classList.add(isAsc ? 'sort-desc' : 'sort-asc');

    rows.sort(function(a, b) {
        let aVal = a.cells[colIndex]?.textContent.trim() || '';
        let bVal = b.cells[colIndex]?.textContent.trim() || '';

        // Parse numbers (format français: espace séparateur, virgule décimale)
        const aNum = parseFloat(aVal.replace(/\s/g, '').replace(',', '.').replace('%', '').replace('+', ''));
        const bNum = parseFloat(bVal.replace(/\s/g, '').replace(',', '.').replace('%', '').replace('+', ''));

        if (!isNaN(aNum) && !isNaN(bNum)) {
            return (aNum - bNum) * direction;
        }
        return aVal.localeCompare(bVal, 'fr') * direction;
    });

    rows.forEach(row => tbody.appendChild(row));
}

// ---- Chart Helpers ----
const CHART_COLORS = {
    primary: '#2563eb',
    primaryLight: '#3b82f6',
    secondary: '#64748b',
    success: '#10b981',
    danger: '#ef4444',
    warning: '#f59e0b',
    info: '#0ea5e9',
    purple: '#8b5cf6',
    grid: '#334155',
    text: '#94a3b8',
};

const CHART_DEFAULTS = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: {
            labels: { color: CHART_COLORS.text, font: { family: 'Inter', size: 12 } }
        },
        tooltip: {
            backgroundColor: '#1e2235',
            borderColor: '#2a2e3e',
            borderWidth: 1,
            titleColor: '#e8eaed',
            bodyColor: '#8b8fa3',
            cornerRadius: 8,
            padding: 12,
        }
    },
    scales: {
        x: {
            grid: { color: 'rgba(42,46,62,0.5)' },
            ticks: { color: CHART_COLORS.text, font: { size: 11 } }
        },
        y: {
            grid: { color: 'rgba(42,46,62,0.5)' },
            ticks: { color: CHART_COLORS.text, font: { size: 11 } }
        }
    }
};

function createLineChart(canvasId, labels, datasets, options = {}) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    return new Chart(ctx, {
        type: 'line',
        data: { labels, datasets },
        options: { ...CHART_DEFAULTS, ...options }
    });
}

function createBarChart(canvasId, labels, datasets, options = {}) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    return new Chart(ctx, {
        type: 'bar',
        data: { labels, datasets },
        options: { ...CHART_DEFAULTS, ...options }
    });
}

function createDoughnutChart(canvasId, labels, data, colors) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    return new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels,
            datasets: [{
                data,
                backgroundColor: colors,
                borderWidth: 0,
                hoverOffset: 8,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'right',
                    labels: { color: CHART_COLORS.text, font: { family: 'Inter', size: 12 }, padding: 12 }
                }
            },
            cutout: '65%',
        }
    });
}

// ---- Number formatting ----
function formatNumber(val, decimals = 0) {
    if (val === null || val === undefined) return '-';
    const num = parseFloat(val);
    if (isNaN(num)) return '-';
    return num.toLocaleString('fr-FR', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function formatPct(val, decimals = 2) {
    if (val === null || val === undefined) return '-';
    const num = parseFloat(val);
    if (isNaN(num)) return '-';
    const sign = num > 0 ? '+' : '';
    return sign + num.toLocaleString('fr-FR', { minimumFractionDigits: decimals, maximumFractionDigits: decimals }) + '%';
}

// ---- CSV Export ----
function exportTableToCSV(tableId, filename) {
    const table = document.getElementById(tableId);
    if (!table) return;

    let csv = [];
    const rows = table.querySelectorAll('tr');
    rows.forEach(row => {
        const cols = row.querySelectorAll('th, td');
        const rowData = Array.from(cols).map(col => '"' + col.textContent.trim().replace(/"/g, '""') + '"');
        csv.push(rowData.join(';'));
    });

    const blob = new Blob(['\ufeff' + csv.join('\n')], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename || 'export.csv';
    link.click();
    URL.revokeObjectURL(link.href);
}

// ---- Fetch helper ----
async function fetchJSON(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
}

// Namespace exposé pour permettre aux templates enfants de supprimer
// leurs redéfinitions locales de formatNumber/formatPct/etc.
window.LookUp = {
    formatNumber, formatPct, fetchJSON,
    createLineChart, createBarChart, createDoughnutChart,
    sortTable, exportTableToCSV,
    CHART_COLORS, CHART_DEFAULTS,
};

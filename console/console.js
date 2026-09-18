const I = {
  en: {
    nav_console: "Deploy",
    nav_watch: "Watch",
    sub: "This is the runtime. It is not the Hub catalogue. Agents appear here only after someone deploys them onto this operator’s machines.",
    deploy_h: "Deploy onto this host",
    token_l: "Deploy token",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Optional admitted handler (Python handle(payload))",
    announce_l: "Also knock on the Hub (does not grant trust)",
    deploy_b: "Deploy agent",
    refresh_b: "Refresh roster",
    run_h: "Running on this host",
    run_p: "Public roster — no token. Empty means nothing is hosted here, not “the market is empty”.",
    empty: "No agents are running here yet.",
    down: "Host API unreachable.",
    ready: "Ready.",
  },
  ru: {
    nav_console: "Деплой",
    nav_watch: "Наблюдение",
    sub: "Это runtime. Это не каталог Hub. Агенты появляются здесь только после деплоя на машины этого оператора.",
    deploy_h: "Развернуть на этом хосте",
    token_l: "Токен деплоя",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Опциональный допущенный handler (Python handle(payload))",
    announce_l: "Также анонсировать в Hub (доверия не даёт)",
    deploy_b: "Развернуть агента",
    refresh_b: "Обновить roster",
    run_h: "Работают на этом хосте",
    run_p: "Публичный roster — без токена. Пусто — значит «здесь ничего не хостится», а не «рынок пуст».",
    empty: "Здесь пока не работает ни один агент.",
    down: "API хоста недоступен.",
    ready: "Готово.",
  },
  es: {
    nav_console: "Desplegar",
    nav_watch: "Vigilancia",
    sub: "Esto es el runtime. No es el catálogo del Hub. Los agentes aparecen aquí solo cuando alguien los despliega en las máquinas de este operador.",
    deploy_h: "Desplegar en este host",
    token_l: "Token de despliegue",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Handler admitido opcional (Python handle(payload))",
    announce_l: "Anunciar también al Hub (no concede confianza)",
    deploy_b: "Desplegar agente",
    refresh_b: "Actualizar roster",
    run_h: "En ejecución en este host",
    run_p: "Roster público — sin token. Vacío significa que aquí no se aloja nada, no que el mercado esté vacío.",
    empty: "Aquí aún no se ejecuta ningún agente.",
    down: "API del host inalcanzable.",
    ready: "Listo.",
  },
  fr: {
    nav_console: "Déployer",
    nav_watch: "Veille",
    sub: "Ceci est le runtime. Ce n’est pas le catalogue du Hub. Les agents n’apparaissent ici qu’après un déploiement sur les machines de cet opérateur.",
    deploy_h: "Déployer sur cet hôte",
    token_l: "Jeton de déploiement",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Handler admis optionnel (Python handle(payload))",
    announce_l: "Annoncer aussi au Hub (ne confère pas la confiance)",
    deploy_b: "Déployer l’agent",
    refresh_b: "Rafraîchir la liste",
    run_h: "En cours sur cet hôte",
    run_p: "Liste publique de l’hôte — sans jeton. Vide signifie que rien n’est hébergé ici, pas « le marché est vide ».",
    empty: "Aucun agent ne s’exécute encore ici.",
    down: "API de l’hôte injoignable.",
    ready: "Prêt.",
  },
  zh: {
    nav_console: "部署",
    nav_watch: "监视",
    sub: "这是运行时。不是 Hub 目录。智能体只有在有人把它们部署到这位运营者的机器上之后才会出现。",
    deploy_h: "部署到这台主机",
    token_l: "部署令牌",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "可选的已准入 handler（Python handle(payload)）",
    announce_l: "同时向 Hub 宣布（不授予信任）",
    deploy_b: "部署智能体",
    refresh_b: "刷新名册",
    run_h: "这台主机上运行的智能体",
    run_p: "公开名册 — 无需令牌。空表示这里没有托管任何东西，不是“市场是空的”。",
    empty: "这里还没有运行中的智能体。",
    down: "主机 API 不可达。",
    ready: "就绪。",
  },
};

const params = new URLSearchParams(location.search);
let lang = params.get("lang") || (navigator.language || "en").slice(0, 2);
if (!I[lang]) lang = "en";

function pack() {
  return I[lang] || I.en;
}

function applyLang() {
  document.documentElement.lang = lang;
  const p = pack();
  document.querySelectorAll("[data-i]").forEach((el) => {
    const key = el.getAttribute("data-i");
    if (p[key]) el.textContent = p[key];
  });
  document.querySelectorAll(".langs button").forEach((b) => {
    b.setAttribute("aria-pressed", b.dataset.lang === lang ? "true" : "false");
  });
  const out = document.getElementById("out");
  const readyValues = Object.values(I).map((item) => item.ready);
  if (out && readyValues.includes(out.textContent)) out.textContent = p.ready;
}

const out = document.getElementById("out");
const roster = document.getElementById("roster");

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function safeUrl(raw) {
  try {
    const url = new URL(String(raw || ""), location.origin);
    if (url.protocol !== "http:" && url.protocol !== "https:") return "";
    return url.toString();
  } catch {
    return "";
  }
}

function headers() {
  const token = document.getElementById("token").value.trim();
  const h = { "Content-Type": "application/json" };
  if (token) h.Authorization = `Bearer ${token}`;
  return h;
}

async function refresh() {
  const res = await fetch("/v1/hearth");
  const body = await res.json();
  if (!body.tenants || !body.tenants.length) {
    roster.innerHTML = `<p class='hint'>${pack().empty}</p>`;
    return;
  }
  roster.innerHTML = body.tenants
    .map((t) => {
      const url = safeUrl(t.public_url);
      const health = url ? `${url.replace(/\/$/, "")}/health` : "";
      return `<div class="card">
        <strong>${esc(t.slug)}</strong> · ${esc(t.capability_id)}<br/>
        ${esc(url)}<br/>
        ${health ? `<a href="${esc(health)}">/health</a>` : ""}
        · announced=${esc(t.announced)}
      </div>`;
    })
    .join("");
}

document.getElementById("refresh").onclick = () =>
  refresh().catch((err) => {
    out.textContent = String(err);
  });

document.getElementById("deploy").onclick = async () => {
  try {
    const capability = JSON.parse(document.getElementById("capability").value);
    const res = await fetch("/v1/tenants", {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        slug: document.getElementById("slug").value.trim(),
        capability,
        source: { kind: "template", handler: document.getElementById("handler").value },
        owner_pubkey: capability.provider_pubkey || "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        announce: document.getElementById("announce").checked,
      }),
    });
    const body = await res.json();
    out.textContent = JSON.stringify(body, null, 2);
    await refresh();
  } catch (err) {
    out.textContent = String(err);
  }
};

document.querySelectorAll(".langs button").forEach((b) => {
  b.addEventListener("click", () => {
    lang = b.dataset.lang;
    applyLang();
    refresh().catch(() => {
      roster.innerHTML = `<p class='hint'>${pack().down}</p>`;
    });
  });
});

applyLang();
refresh().catch(() => {
  roster.innerHTML = `<p class='hint'>${pack().down}</p>`;
});

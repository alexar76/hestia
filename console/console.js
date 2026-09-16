const I = {
  en: {
    nav_console: "Deploy",
    nav_watch: "Watch",
    sub: "This is the runtime. It is not the Hub catalogue. Agents appear here only after someone deploys them onto this operator’s machines.",
    deploy_h: "Deploy onto this hearth",
    token_l: "Deploy token",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Optional admitted handler (Python handle(payload))",
    announce_l: "Also knock on the Hub (does not grant trust)",
    deploy_b: "Light the hearth",
    refresh_b: "Refresh roster",
    run_h: "Running on this host",
    run_p: "Public roster — no token. Empty means nothing is hosted here, not “the market is empty”.",
    empty: "Nothing is burning here yet.",
    down: "Hearth API unreachable.",
    ready: "Ready.",
  },
  ru: {
    nav_console: "Деплой",
    nav_watch: "Наблюдение",
    sub: "Это runtime. Это не каталог Hub. Агенты появляются здесь только после деплоя на машины этого оператора.",
    deploy_h: "Задеплоить на этот очаг",
    token_l: "Токен деплоя",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Опциональный допущенный handler (Python handle(payload))",
    announce_l: "Также стукнуть в Hub (не выдаёт доверие)",
    deploy_b: "Зажечь очаг",
    refresh_b: "Обновить roster",
    run_h: "Крутится на этом хосте",
    run_p: "Публичный roster — без токена. Пусто значит «здесь ничего не хостится», а не «рынок пуст».",
    empty: "Здесь пока ничего не горит.",
    down: "API очага недоступен.",
    ready: "Готово.",
  },
  es: {
    nav_console: "Desplegar",
    nav_watch: "Vigilancia",
    sub: "Esto es el runtime. No es el catálogo del Hub. Los agentes aparecen aquí solo cuando alguien los despliega en las máquinas de este operador.",
    deploy_h: "Desplegar en este hogar",
    token_l: "Token de despliegue",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Handler admitido opcional (Python handle(payload))",
    announce_l: "También golpear el Hub (no concede confianza)",
    deploy_b: "Encender el hogar",
    refresh_b: "Actualizar roster",
    run_h: "En ejecución en este host",
    run_p: "Roster público — sin token. Vacío significa que aquí no se aloja nada, no que el mercado esté vacío.",
    empty: "Aún no arde nada aquí.",
    down: "API del hogar inalcanzable.",
    ready: "Listo.",
  },
  fr: {
    nav_console: "Déployer",
    nav_watch: "Veille",
    sub: "Ceci est le runtime. Ce n’est pas le catalogue du Hub. Les agents n’apparaissent ici qu’après un déploiement sur les machines de cet opérateur.",
    deploy_h: "Déployer sur cet âtre",
    token_l: "Jeton de déploiement",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "Handler admis optionnel (Python handle(payload))",
    announce_l: "Frapper aussi le Hub (ne confère pas la confiance)",
    deploy_b: "Allumer l’âtre",
    refresh_b: "Rafraîchir le roster",
    run_h: "En cours sur cet hôte",
    run_p: "Roster public — sans jeton. Vide signifie que rien n’est hébergé ici, pas « le marché est vide ».",
    empty: "Rien ne brûle encore ici.",
    down: "API de l’âtre injoignable.",
    ready: "Prêt.",
  },
  zh: {
    nav_console: "部署",
    nav_watch: "监视",
    sub: "这是运行时。不是 Hub 目录。智能体只有在有人把它们部署到这位运营者的机器上之后才会出现。",
    deploy_h: "部署到这座炉灶",
    token_l: "部署令牌",
    slug_l: "Slug",
    cap_l: "Capability JSON",
    handler_l: "可选的已准入 handler（Python handle(payload)）",
    announce_l: "同时向 Hub 敲门（不授予信任）",
    deploy_b: "点燃炉灶",
    refresh_b: "刷新名册",
    run_h: "本机正在运行",
    run_p: "公开名册 — 无需令牌。空表示这里没有托管任何东西，不是“市场是空的”。",
    empty: "这里还没有火。",
    down: "炉灶 API 不可达。",
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
    .map(
      (t) => `<div class="card">
        <strong>${t.slug}</strong> · ${t.capability_id}<br/>
        ${t.public_url}<br/>
        <a href="${t.public_url}/health">/health</a>
        · announced=${t.announced}
      </div>`
    )
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

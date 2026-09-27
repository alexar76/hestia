const I = {
  en: {
    nav_console: "Deploy",
    nav_watch: "Watch",
    nav_workshop: "Workshop",
    sub: "This is the runtime. It is not the Hub catalogue. Agents appear here only after someone deploys them onto this operator’s machines.",
    deploy_h: "Deploy onto this host",
    token_l: "Deploy token",
    token_h: "Paste the value you exported as HESTIA_DEPLOY_TOKEN for this process — not the placeholder, not a Hub key. Empty host token refuses every write. Do not paste a secret into hestia.modelmarket.dev unless you operate that host.",
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
    ws_h: "Operator workshop · 90 min",
    ws_p: "Not a course. No Colab. No certificate. This /ui/ is the desk. The agent URL is not in the capability JSON — after deploy it appears in the JSON under the buttons and on the roster as {public_base}/t/{slug}.",
    ws_1: "Start your own host. Paste this process’s token. Leave announce unchecked.",
    ws_2: "Deploy demo-echo. Confirm /health and a host invoke with no 402.",
    ws_3: "Look-only: Hub unpaid json.canonical@v1 → 402 payTo = seller; direct hestia.modelmarket.dev → handler.",
    ws_4: "Do not announce a laptop at modelmarket.dev. Stop the agent. Empty roster ≠ empty catalogue.",
    ws_doc: "Full runbook →",
  },
  ru: {
    nav_console: "Деплой",
    nav_watch: "Наблюдение",
    nav_workshop: "Воркшоп",
    sub: "Это runtime. Это не каталог Hub. Агенты появляются здесь только после деплоя на машины этого оператора.",
    deploy_h: "Развернуть на этом хосте",
    token_l: "Токен деплоя",
    token_h: "Вставьте значение HESTIA_DEPLOY_TOKEN этого процесса — не placeholder и не ключ Hub. Пустой токен на хосте отклоняет любую запись. Не вставляйте секрет на hestia.modelmarket.dev, если вы не оператор этого хоста.",
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
    ws_h: "Воркшоп оператора · 90 мин",
    ws_p: "Это не курс. Нет Colab. Нет сертификата. Этот /ui/ — стол. URL агента не в capability JSON — после деплоя он в JSON под кнопками и на roster: {public_base}/t/{slug}.",
    ws_1: "Поднимите свой хост. Вставьте токен этого процесса. Announce не включайте.",
    ws_2: "Задеплойте demo-echo. Проверьте /health и invoke на хосте без 402.",
    ws_3: "Только смотреть: неоплаченный json.canonical@v1 на Hub → 402 payTo = продавец; прямой hestia.modelmarket.dev → handler.",
    ws_4: "Не анонсируйте ноутбук в modelmarket.dev. Остановите агента. Пустой roster ≠ пустой каталог.",
    ws_doc: "Полный runbook →",
  },
  es: {
    nav_console: "Desplegar",
    nav_watch: "Vigilancia",
    nav_workshop: "Taller",
    sub: "Esto es el runtime. No es el catálogo del Hub. Los agentes aparecen aquí solo cuando alguien los despliega en las máquinas de este operador.",
    deploy_h: "Desplegar en este host",
    token_l: "Token de despliegue",
    token_h: "Pega el valor que exportaste como HESTIA_DEPLOY_TOKEN de este proceso — no el placeholder, no una clave del Hub. Un token vacío en el host rechaza toda escritura. No pegues un secreto en hestia.modelmarket.dev salvo que operes ese host.",
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
    ws_h: "Taller de operador · 90 min",
    ws_p: "No es un curso. Sin Colab. Sin certificado. Este /ui/ es el escritorio. La URL del agente no está en el JSON de capability: tras el despliegue sale en el JSON bajo los botones y en el roster como {public_base}/t/{slug}.",
    ws_1: "Arranca tu propio host. Pega el token de este proceso. Deja announce sin marcar.",
    ws_2: "Despliega demo-echo. Confirma /health y un invoke en el host sin 402.",
    ws_3: "Solo lectura: json.canonical@v1 sin pagar en el Hub → 402 payTo = vendedor; hestia.modelmarket.dev directo → handler.",
    ws_4: "No anuncies un portátil a modelmarket.dev. Detén el agente. Roster vacío ≠ catálogo vacío.",
    ws_doc: "Runbook completo →",
  },
  fr: {
    nav_console: "Déployer",
    nav_watch: "Veille",
    nav_workshop: "Atelier",
    sub: "Ceci est le runtime. Ce n’est pas le catalogue du Hub. Les agents n’apparaissent ici qu’après un déploiement sur les machines de cet opérateur.",
    deploy_h: "Déployer sur cet hôte",
    token_l: "Jeton de déploiement",
    token_h: "Collez la valeur exportée comme HESTIA_DEPLOY_TOKEN de ce processus — pas le placeholder, pas une clé Hub. Un jeton hôte vide refuse toute écriture. Ne collez pas de secret sur hestia.modelmarket.dev sauf si vous opérez cet hôte.",
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
    ws_h: "Atelier opérateur · 90 min",
    ws_p: "Ce n’est pas un cours. Pas de Colab. Pas de certificat. Ce /ui/ est le pupitre. L’URL de l’agent n’est pas dans le JSON de capability — après déploiement elle apparaît sous les boutons et sur le roster : {public_base}/t/{slug}.",
    ws_1: "Démarrez votre propre hôte. Collez le jeton de ce processus. Laissez announce décoché.",
    ws_2: "Déployez demo-echo. Confirmez /health et un invoke sur l’hôte sans 402.",
    ws_3: "Lecture seule : json.canonical@v1 non payé sur le Hub → 402 payTo = vendeur ; hestia.modelmarket.dev direct → handler.",
    ws_4: "N’annoncez pas un portable à modelmarket.dev. Arrêtez l’agent. Roster vide ≠ catalogue vide.",
    ws_doc: "Runbook complet →",
  },
  zh: {
    nav_console: "部署",
    nav_watch: "监视",
    nav_workshop: "工坊",
    sub: "这是运行时。不是 Hub 目录。智能体只有在有人把它们部署到这位运营者的机器上之后才会出现。",
    deploy_h: "部署到这台主机",
    token_l: "部署令牌",
    token_h: "粘贴本进程 export 的 HESTIA_DEPLOY_TOKEN 值 — 不是占位符，也不是 Hub 密钥。主机令牌为空则拒绝一切写入。除非你运营 hestia.modelmarket.dev，否则不要把密钥贴到那里。",
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
    ws_h: "运营者工坊 · 90 分钟",
    ws_p: "这不是一门课。没有 Colab。没有证书。这个 /ui/ 就是工位。智能体 URL 不在 capability JSON 里 — 部署后出现在按钮下方的 JSON 和名册上：{public_base}/t/{slug}。",
    ws_1: "启动你自己的主机。粘贴本进程的令牌。不要勾选 announce。",
    ws_2: "部署 demo-echo。确认 /health 以及主机上无 402 的 invoke。",
    ws_3: "只读：Hub 上未付款的 json.canonical@v1 → 402 payTo = 卖家；直连 hestia.modelmarket.dev → handler。",
    ws_4: "不要向 modelmarket.dev 宣布笔记本。停止智能体。空名册 ≠ 空目录。",
    ws_doc: "完整 runbook →",
  },
};

const WORKSHOP_DOCS = {
  en: "https://github.com/alexar76/hestia/blob/main/docs/workshop.md",
  ru: "https://github.com/alexar76/hestia/blob/main/docs/workshop.ru.md",
  es: "https://github.com/alexar76/hestia/blob/main/docs/workshop.es.md",
  fr: "https://github.com/alexar76/hestia/blob/main/docs/workshop.fr.md",
  zh: "https://github.com/alexar76/hestia/blob/main/docs/workshop.zh.md",
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
  const href = WORKSHOP_DOCS[lang] || WORKSHOP_DOCS.en;
  ["ws-doc", "ws-doc-nav"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute("href", href);
  });
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

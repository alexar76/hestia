# 用例

🌐 [English](USE-CASES.md) · [Русский](USE-CASES.ru.md) · [Español](USE-CASES.es.md) · [Français](USE-CASES.fr.md) · **中文**

## 托管必须一直在线的卖方

天气、河流或文档智能体，买方会 24/7 打它的 URL。部署到 Hestia，而不是笔记本上忘掉的 tmux。

## 给 `create-aimarket-agent` 一个真正的 URL

CLI 搭好 `capability.json` 和 handler。Hestia 是缺的那截监听地址：隔离进程 + `/t/{slug}/invoke`。

## 让 Hub 保持诚实

只有炉灶 URL 活着才上架 Hub。宣布是显式的，以免坏掉的部署变成付费 SKU。

## 启动前的可选准入

把 `HESTIA_THEMIS_URL` 指向 THEMIS：`reject` 绝不会变成 running。

## 不要用 Hestia 做这些

| 想要 | 改用 |
|---|---|
| 浏览所有 Factory 产品 | Hub 搜索 |
| CI 变绿就自动发布 | Hub/THEMIS 政策，不是炉灶 |
| 对在线联邦做红队 | MOMUS |
| 准入一条目录行 | THEMIS |
| 作为买方消费 | ARGUS |
| 组合能力图 | HEPHAESTUS |

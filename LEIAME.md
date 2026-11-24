# Semáforo dos Clientes

Painel que mostra, em verde, amarelo ou vermelho, se os sites dos clientes estão no ar.
Roda de graça no GitHub: a cada ~5 minutos o GitHub consulta todos os links e atualiza o painel.

## Cores

| Cor | Significa |
|---|---|
| 🟢 Verde | Respondeu HTTP 200, rápido e com certificado em dia |
| 🟡 Amarelo | Respondeu, mas está lento (mais de 3 s), o certificado SSL vence em menos de 15 dias, redirecionou para outro endereço ou respondeu 401/403 |
| 🔴 Vermelho | Não respondeu em 20 s, erro de servidor (5xx), página não encontrada (404) ou certificado vencido/inválido |

## Adicionar ou remover sites

Edite `links_clientes.txt` direto no GitHub (ícone de lápis) e salve. O painel se atualiza sozinho.

```
CLIENTE NOME
https://site-de-producao.com.br
https://sitehmg.com.br
```

Links com `hmg` ou `hom` no endereço aparecem como **Homologação**; os demais como **Produção**.

## Alertas no Telegram

Em **Settings → Secrets and variables → Actions → New repository secret**, crie:

- `TELEGRAM_TOKEN`: o token que o @BotFather enviou
- `TELEGRAM_CHAT_ID`: o id do seu chat ou do grupo

O alerta chega quando um site fica vermelho em 2 checagens seguidas (evita alarme falso) e outro quando ele volta.

## Rodar na hora

Aba **Actions → Monitorar sites → Run workflow**.

## Testar no computador

```
python check.py
python -m http.server -d site 8000
```
Depois abra http://localhost:8000

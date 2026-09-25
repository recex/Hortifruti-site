# O Camponês — backend

Site + painel do dono + banco de dados de verdade (SQLite), tudo num único
serviço Flask. Resolve o problema de sincronização entre aparelhos: agora
produtos, pedidos, cupons etc. ficam guardados no servidor, não no navegador
de cada pessoa.

## Rodando localmente (pra testar antes de subir)

```bash
pip install -r requirements.txt
python3 app.py
```

Abra http://127.0.0.1:5000 — o banco `campones.db` é criado automaticamente
na primeira execução, com a senha do painel `hortifruti123` (troque assim
que possível, tem um campo pra isso dentro do próprio painel).

## Deploy no Render

1. Suba esta pasta inteira (`app.py`, `requirements.txt`, `templates/`,
   `static/`, `render.yaml`) pro seu repositório Git.
2. No Render: **New +** → **Web Service** (ou **Blueprint**, se usar o
   `render.yaml` incluído — ele já configura tudo sozinho).
3. Runtime: **Python 3**.
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `gunicorn app:app`
6. Nas variáveis de ambiente, defina:
   - `SECRET_KEY`: qualquer texto aleatório longo (o `render.yaml` já gera
     um sozinho se você usar o Blueprint)
   - `ADMIN_PASSWORD`: a senha que você quer usar no painel (só é lida na
     primeira vez que o banco é criado — depois disso, troque pelo próprio
     painel)

## ⚠️ Sobre persistência de dados no plano gratuito do Render

Isso é importante: **serviços Web gratuitos do Render têm disco
temporário**. Sempre que o serviço "dorme" por inatividade (15 min sem
acesso) ou é reimplantado, o arquivo `campones.db` é apagado — ou seja, os
produtos e pedidos cadastrados somem.

Três caminhos, do mais simples ao mais robusto:

1. **Só testar / mostrar pro cliente**: pode usar como está, sabendo que os
   dados não são permanentes.
2. **Usar de verdade, gastando pouco**: assine um plano pago do Render
   (a partir de uns US$7/mês) e adicione um **Persistent Disk** apontando
   pro caminho onde o `campones.db` fica salvo. Aí os dados sobrevivem a
   reinicializações.
3. **Usar de verdade, de graça**: trocar o SQLite por um Postgres gratuito
   de outro provedor que não expira, como o [Neon](https://neon.tech) ou o
   [Supabase](https://supabase.com). Isso exige adaptar `app.py` pra falar
   com Postgres em vez de SQLite (dá pra pedir ajuda nisso quando quiser
   seguir esse caminho).

## O que mudou em relação à versão anterior (só HTML)

- Pedidos, produtos, cupons, cadastro de bairros/horários, configurações
  de Pix/WhatsApp/rodapé — tudo isso agora vive no banco de dados do
  servidor, então **qualquer pessoa que acessar o site vê os mesmos dados**,
  e o painel do dono funciona em qualquer aparelho.
- Os avisos (Discord/Slack/Telegram/genérico) agora são disparados **pelo
  servidor**, não pelo navegador do cliente — mais seguro (a URL do webhook
  nunca fica exposta) e não depende mais de CORS.
- O preço de cada item do pedido é sempre recalculado no servidor a partir
  do catálogo — o navegador do cliente não consegue mais "forjar" um total
  mais barato editando o código.
- Login do painel agora usa sessão de verdade (cookie assinado) com senha
  criptografada no banco, em vez de senha visível no código-fonte.

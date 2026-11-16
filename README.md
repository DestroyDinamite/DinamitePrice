# 💰 Monitor de Preços 24h

Você manda o link de um produto e recebe um aviso no celular quando o preço **subir ou baixar**.
Ele roda **24 horas por dia, de graça**, na nuvem do GitHub, sem precisar deixar o computador ligado.
Os preços são conferidos a cada 5 minutos.

---

## Configuração (uma vez, uns 10 minutos)

### 1. Coloque o projeto no GitHub
1. Crie uma conta em https://github.com, se ainda não tiver.
2. Crie um repositório novo (pode ser **privado**), por exemplo `monitor-precos`.
3. Envie para ele todos os arquivos desta pasta, inclusive a pasta `.github`.

### 2. Escolha onde receber os avisos

**Opção A: ntfy, a mais fácil (notificação push no celular, sem cadastro)**
1. Instale o app **ntfy** (Android ou iPhone).
2. No app, toque em **+** e assine um tópico com um nome difícil de adivinhar, ex.: `precos-rafa-8f3k2`.
3. No GitHub: **Settings → Secrets and variables → Actions → New repository secret**
   - Nome: `NTFY_TOPICO` · Valor: `precos-rafa-8f3k2`

**Opção B: Telegram**
1. No Telegram, fale com **@BotFather**, envie `/newbot` e copie o **token**.
2. Mande qualquer mensagem para o seu bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para descobrir o seu `chat.id`.
3. Crie os secrets `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID`.

**Opção C: E-mail (Gmail)**
Crie uma "senha de app" em https://myaccount.google.com/apppasswords e cadastre os secrets:
`SMTP_HOST`=`smtp.gmail.com`, `SMTP_USUARIO`=seu e-mail, `SMTP_SENHA`=a senha de app, `EMAIL_PARA` (opcional).

Você pode ativar mais de um canal ao mesmo tempo.

### 3. Ative e teste
1. Vá na aba **Actions** do repositório e, se o GitHub pedir, clique em **Enable workflows**.
2. Clique em **Monitor de Preços → Run workflow**, escolha a ação `test-notify` e rode.
3. Você deve receber "Teste do Monitor de Preços ✅" no celular.

---

## Uso no dia a dia (dá para fazer pelo celular)

**Adicionar um produto:** em **Actions → Monitor de Preços → Run workflow**, escolha `add`, cole o **link** e rode.

**Remover:** escolha `remove` e informe o **id**. Os ids ficam no arquivo `produtos.json`.

O histórico de preços de cada produto fica salvo em `produtos.json`.

Exemplo de aviso:
```
📉 Baixou: Notebook Lenovo IdeaPad
De R$ 3.499,00 para R$ 2.999,00 (-14.3%)
🏆 Menor preço já registrado!
```

---

## Monitoramento por busca (todas as placas de um modelo)

O arquivo `buscas.json` define buscas que acompanham **todos os anúncios** de um modelo numa loja.
Hoje ele cobre:
- **Kabum:** todas as RTX 5070 Ti e todas as RTX 5080, inclusive as de vendedores parceiros
- **Setup Ninja (Mercado Livre):** RTX 5070 Ti e RTX 5080 da vitrine da loja
- **Pato Loco:** RTX 5070 Ti e RTX 5080 da categoria de placas de vídeo

Você recebe aviso quando:
- 📉📈 o preço à vista (PIX) muda
- 🆕 aparece um anúncio novo
- ✅ um produto volta ao estoque
- ⛔ um produto esgota

Se houver mais de 6 mudanças de uma vez, chega uma única notificação com o resumo.
Para ver a lista atual com os preços, rode `python monitor.py buscas`, ou abra o `buscas.json` no GitHub.

Lojas testadas que **bloqueiam** o acesso a partir do GitHub: Amazon, Netshoes, Pichau, Magalu e Terabyte.

## Usando no próprio computador (opcional)

```bash
pip install -r requirements.txt
python monitor.py add "https://link-do-produto"
python monitor.py list
python monitor.py run --minutos 5      # fica conferindo enquanto o PC estiver ligado
```
Para receber notificações localmente, defina as mesmas variáveis (ex.: `export NTFY_TOPICO=...`).

---

## Se o preço não for encontrado

O app reconhece a maioria das lojas porque lê os dados estruturados (schema.org), as meta tags de preço
e os padrões de lojas conhecidas (Amazon, Mercado Livre etc.). Se uma loja não funcionar, informe o
**seletor CSS** do preço:
1. No computador, abra o produto, clique com o botão direito no preço e escolha **Inspecionar**.
2. Clique com o botão direito no elemento destacado e escolha **Copiar → Copiar seletor**.
3. Use o seletor no campo `seletor` ao adicionar o produto.

## Observações
- O GitHub pode atrasar execuções agendadas alguns minutos em horários de pico. Na prática, o aviso chega entre 5 e 15 minutos depois da mudança.
- O GitHub pausa agendamentos de repositórios **sem nenhum commit por 60 dias**. Se receber o e-mail de aviso, basta clicar para reativar.
- Algumas lojas bloqueiam acessos automáticos, ou só mostram o preço depois de carregar JavaScript. Nesses casos o produto aparece como "preço não encontrado".

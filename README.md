# 💰 DinamitePrice: Monitor de Preços

Monitora o preço de placas de vídeo (RTX 5070 Ti e RTX 5080) em várias lojas e avisa no celular, pelo app **ntfy**, quando:
- 📉📈 o preço muda
- 🆕 aparece um anúncio novo
- ✅ um produto volta ao estoque
- ⛔ um produto esgota

Roda **no seu notebook**, conferindo a cada 5 minutos, em segundo plano e sem abrir o navegador.

---

## Instalação no notebook (Windows)

### 1. Instale o Python
Abra o **PowerShell** e rode:
```powershell
winget install -e --id Python.Python.3.12
```
Feche e abra o PowerShell de novo. Para conferir, rode `python --version`.
(Sem o winget: baixe em python.org e, na instalação, marque **"Add python.exe to PATH"**.)

### 2. Baixe o DinamitePrice
Baixe o ZIP em https://github.com/DestroyDinamite/DinamitePrice/archive/refs/heads/main.zip e extraia para **`C:\DinamitePrice`**
(a pasta deve conter o `monitor.py` direto dentro dela).

### 3. Instale as dependências e configure o aviso
```powershell
cd C:\DinamitePrice
python -m pip install -r requirements.txt
Set-Content config.env "NTFY_TOPICO=SEU_TOPICO_DO_NTFY"
python monitor.py test-notify
```
A notificação de teste deve chegar no celular.

### 4. Teste uma conferência
```powershell
python monitor.py check
python monitor.py buscas
```

### 5. Deixe rodando sozinho, escondido, sempre que ligar o notebook
```powershell
$acao = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument "monitor.py run --minutos 5" -WorkingDirectory "C:\DinamitePrice"
$gatilho = New-ScheduledTaskTrigger -AtLogOn
$cfg = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "DinamitePrice" -Action $acao -Trigger $gatilho -Settings $cfg
Start-ScheduledTask -TaskName "DinamitePrice"
```
O que acontece fica registrado em `C:\DinamitePrice\monitor.log`.

### 6. Não deixar o notebook dormir (na tomada)
```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 5
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
```
Isso faz o notebook nunca suspender na tomada, apagar a tela depois de 5 minutos e não fazer nada ao fechar a tampa.
Ative também o **limite de carga da bateria** no app da marca do notebook (60–80%).

### Atualizar o app (quando houver melhorias ou lojas novas)
```powershell
cd C:\DinamitePrice
python monitor.py atualizar
Stop-ScheduledTask DinamitePrice; Start-ScheduledTask DinamitePrice
```
Isso baixa o código novo e inclui buscas novas sem apagar o histórico de preços.

### Comandos úteis
| Comando | O que faz |
|---|---|
| `python monitor.py buscas` | lista todas as placas e preços atuais |
| `python monitor.py add "<link>"` | monitora um produto específico |
| `python monitor.py list` / `remove <id>` | lista ou remove produtos específicos |
| `Stop-ScheduledTask -TaskName DinamitePrice` | para o monitor |
| `Start-ScheduledTask -TaskName DinamitePrice` | inicia o monitor |
| `Get-Content monitor.log -Tail 30` | mostra as últimas linhas do log |

---
## Monitoramento por busca (todas as placas de um modelo)

O arquivo `buscas.json` define buscas que acompanham **todos os anúncios** de um modelo numa loja.
Hoje ele cobre:
- **Kabum:** todas as RTX 5070 Ti e todas as RTX 5080, inclusive as de vendedores parceiros
- **Setup Ninja (Mercado Livre):** RTX 5070 Ti e RTX 5080 da vitrine da loja
- **Pato Loco:** RTX 5070 Ti e RTX 5080 da categoria de placas de vídeo
- **TNT Info:** site (busca) e vitrine da loja no Mercado Livre
- **Americanas** e **Ibyte:** busca da loja
- **Terabyte:** busca do site (funciona pela internet de casa)
- **Lojas oficiais no Mercado Livre:** Gigabyte, NVIDIA e Inno3D

Você recebe aviso quando:
- 📉📈 o preço à vista (PIX) muda
- 🆕 aparece um anúncio novo
- ✅ um produto volta ao estoque
- ⛔ um produto esgota

Se houver mais de 6 mudanças de uma vez, chega uma única notificação com o resumo.
Para ver a lista atual com os preços, rode `python monitor.py buscas`.

Lojas que bloquearam o acesso quando o app rodava no GitHub (podem funcionar pela internet de casa, a testar): Amazon, Magalu, Pichau, Terabyte, Netshoes, Casas Bahia e Carrefour.

## Linux / Mac

```bash
pip install -r requirements.txt
python monitor.py add "https://link-do-produto"
python monitor.py list
python monitor.py run --minutos 5      # fica conferindo enquanto o PC estiver ligado
```
Crie o arquivo `config.env` com a linha `NTFY_TOPICO=seu_topico`, igual ao do Windows.

---

## Se o preço não for encontrado

O app reconhece a maioria das lojas porque lê os dados estruturados (schema.org), as meta tags de preço
e os padrões de lojas conhecidas (Amazon, Mercado Livre etc.). Se uma loja não funcionar, informe o
**seletor CSS** do preço:
1. No computador, abra o produto, clique com o botão direito no preço e escolha **Inspecionar**.
2. Clique com o botão direito no elemento destacado e escolha **Copiar → Copiar seletor**.
3. Adicione o produto com `python monitor.py add "<link>" --seletor "<seletor>"`.


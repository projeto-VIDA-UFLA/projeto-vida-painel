# Painel web do VIDA

Tela única para acompanhar e operar o VIDA (LMT/UFLA): modo, segurança, GPS/RTK, bateria, posição em relação à
rota, pendências para o automático, erros e eventos. Abre em **http://localhost:8080**, só no próprio notebook.

## Subir

Precisa da imagem do alto-nível (`vida-ros-ws:humble`), então faça o build dela antes:

```bash
cd ~/projeto-vida/projeto-vida-controle-alto-nivel && docker compose build
cd ~/projeto-vida/projeto-vida-painel && docker compose up -d --build
```

O container usa `restart: unless-stopped`: depois disso, volta sozinho quando o notebook liga. O build baixa
pacotes (precisa de internet); depois, ele roda sem internet.

Refaça o build do painel sempre que o build do alto-nível mudar, porque ele usa as mensagens e a navegação de lá.

## O que o painel faz no carro

Ele só **lê** os tópicos ROS e não publica nada no carro. A troca de modo continua sendo pelo controle.

| Ação | O que executa | Quando é permitida |
|---|---|---|
| Iniciar | `docker compose up` do alto-nível: `navegacao` com a rota selecionada, ou `ros_ws` sem rota | Sem automático, sem manobra, veículo parado, sem gravação |
| Parar | `docker compose stop` do alto-nível | Sempre (menos durante gravação). O ESP32 corta a tração e freia ao perder as referências |
| Trocar a rota | Com o carro rodando, recria a `navegacao` (o gamepad para por alguns segundos) | Como em Iniciar, sempre com confirmação na página |
| Gravar rota | Guarda os fixes do GPS e salva em `analises/<data>/` | Fora do automático |

O acesso ao Docker do notebook equivale a root. Por isso, o servidor escuta só em `127.0.0.1`, recusa outros
`Host`/`Origin`, aceita só as ações acima e só rotas da lista.

## Rotas

- **Lista:** arquivos `vida_path` em `~/projeto-vida/analises/<pasta>/*.csv`, menos os `*_bruta.csv`. Subpastas como `rascunhos/` ficam de fora.
- **Gravação:** usa os fixes de GPS, como a rota 6, não o `path_recorder`: reamostra por arco e suaviza com gaussiana de 1 m. Pontos em RTK NONE são ignorados; FLOAT é aceito (sem saltos em 01/11) e contado no resumo.
- **Fechada ou aberta:** fechada quando termina a até 3 m do começo.
- **Resumo ao salvar:** comprimento, raio mínimo e trecho abaixo de 3,3 m (limite de 20° com entre-eixos de ~1,2 m).

## Regras de exibição

- **Pendências:** repetem o `entry_problem` do `mode_manager`, com os limites lidos do `navigation.yaml`. Se ele mudar, mude também `painel/logic.py`.
- **Bateria:** 36 V = 0% e 38,7 V = 100%, em `painel/logic.py`. A carga só é recalculada depois de 60 s parado e sem tração, porque sob carga a tensão cai. Verde a partir de 50%, amarelo de 20 a 49%, vermelho abaixo de 20%.
- **Limpar erros:** apaga só o histórico da tela. Flags ativas continuam aparecendo até serem resolvidas, e nada é rearmado no carro.
- **Conexão:** o bloco ESP32 / Odroid usa ping no Odroid (`VIDA_ODROID_IP`) e o nome de quem publica `/vida/safety/status` e `/robot_state/status`. Dados de um `rosbag2_player` não contam como conectado, e a barra superior avisa que é reprodução de bag.
- **Dados parados:** viram "sem dados" em vez de manter o último valor.
- **Mapa GPS:** imagens Esri World Imagery, salvas em disco ao selecionar a rota e usadas sem internet. Uma área ainda não salva fica sem foto, com aviso; o Gráfico funciona sempre. Os termos de uso da Esri para guardar imagens ainda precisam ser conferidos.

## Testes

```bash
docker run --rm --network none vida-painel:humble bash -lc \
  'source /opt/ros/humble/setup.bash && source /vida_ws/install/setup.bash && cd /painel && python3 -m pytest -q tests'
```

Para testar sem o carro, use o simulador num domínio ROS separado, sem tocar no Docker:

```bash
docker run -d --rm --name vida_sim --network host -e ROS_DOMAIN_ID=77 -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  -v ~/projeto-vida/analises:/caminhos:ro vida-ros-ws:humble bash -lc \
  'source /opt/ros/humble/setup.bash && source /vida_ws/install/setup.bash && exec ros2 launch vida_navigation simulation.launch.py path_file:=/caminhos/2026-11-01/rota_6.csv'
docker run -d --rm --name vida_painel_teste --network host -e ROS_DOMAIN_ID=77 -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  -e VIDA_PAINEL_PORT=18080 -e VIDA_PAINEL_DRY_RUN=1 -e VIDA_PAINEL_DATA=/tmp/d \
  -v ~/projeto-vida/analises:/caminhos:ro vida-painel:humble
```

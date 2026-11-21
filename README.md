# Projeto VIDA: painel de operação

Painel web de **operação e monitoramento** do **VIDA (Veículo Inteligente de Desenvolvimento
Aplicado)**, protótipo de veículo autônomo do Laboratório de Mobilidade Terrestre (LMT) da UFLA.

Uma tela única, aberta no navegador do notebook de operação, que mostra o estado de todo o
veículo e conduz a operação: iniciar e parar o carro, escolher e gravar rotas, gravar e
reproduzir registros (*bags*) e conectar o notebook à Odroid.

- **Endereço:** http://localhost:8080, no próprio notebook
- **Visão geral do projeto e como operar o veículo:** [projeto-VIDA-UFLA](https://github.com/projeto-VIDA-UFLA)

## Lugar na arquitetura

O painel roda no notebook (nível 3), ao lado do
[projeto-vida-controle-alto-nivel](https://github.com/projeto-VIDA-UFLA/projeto-vida-controle-alto-nivel).
Ele é um nó ROS 2 que **só assina tópicos**: lê os dados do ESP32, da Odroid e do notebook e não
publica nada no carro. A troca de modo (manual e automático) continua sendo feita pelo gamepad.

## O que o painel mostra

| Área | Conteúdo |
|---|---|
| Barra de estado | Segurança, conexão ESP32 / Odroid, RTK, correções RTK, EKF e bateria |
| Posição do veículo | Rota, posição, orientação e rastro, em gráfico local ou sobre imagem de satélite |
| Segurança | Enables físico e lógico, validade das referências e motivos de bloqueio do ESP32 |
| Veículo | Velocidade e esterçamento (referência e medição), PWM dos motores e freio |
| Navegação | Modo de operação, progresso na rota, erro lateral e erro de rumo |
| Condições do automático | Lista de verificação para entrar no modo automático e o próximo passo |
| Bateria | Tensão, corrente e carga estimada |
| Erros, eventos e saúde dos dados | Erros dos nós, linha do tempo da operação e frequência de cada fonte de dados |

## O que o painel comanda

| Ação | O que executa | Quando é permitida |
|---|---|---|
| Iniciar | Sobe o serviço `navegacao` do alto nível com a rota selecionada, ou `ros_ws` sem rota | Sem automático, sem manobra, veículo parado, sem gravação de rota |
| Parar | Para os serviços do alto nível; o ESP32 corta a tração e freia ao perder as referências | Sempre, exceto durante a gravação de uma rota |
| Trocar a rota | Com o carro em funcionamento, recria o serviço `navegacao` | Como em Iniciar, com confirmação |
| Gravar rota | Registra os pontos do GNSS enquanto o operador dirige e salva a rota tratada | Fora do automático |
| Bags | Grava todos os tópicos, reproduz, renomeia e exclui registros | Reprodução bloqueia os comandos do carro |
| Conectar / Desconectar da Odroid | Pede ao serviço `vida-rede` do notebook que configure a porta do cabo | Desconectar: com o carro parado |

Ações que alteram o carro ou apagam arquivos pedem confirmação. Rotas e bags excluídos vão para
uma lixeira (`.lixeira`) e podem ser recuperados.

## Instalação

O painel usa a imagem do alto nível (`vida-ros-ws:humble`) como base. Com os dois repositórios
lado a lado em `~/projeto-vida/`:

```bash
cd ~/projeto-vida/projeto-vida-controle-alto-nivel && docker compose build
cd ~/projeto-vida/projeto-vida-painel && docker compose up -d --build
```

O container usa `restart: unless-stopped` e volta sozinho quando o notebook liga. Refaça o build
do painel sempre que o build do alto nível mudar.

| Pasta do notebook | Uso |
|---|---|
| `~/projeto-vida/analises` | Rotas (`<pasta>/<rota>.csv`) |
| `~/projeto-vida/rosbag` | Bags |

## Rotas gravadas pelo painel

- Usa os pontos do GNSS (RTK *fixed* ou *float*; pontos sem RTK são descartados).
- Reamostra a cada 0,1 m, suaviza com filtro gaussiano de 1 m e salva a cada 0,25 m.
- A rota é fechada quando termina a até 3 m do início.
- Ao salvar, informa o comprimento, o menor raio de curva e os trechos abaixo de 3,3 m (limite
  de 20° de esterçamento).
- Salva dois arquivos em `~/projeto-vida/analises/<data>/`: os pontos brutos (`_bruta.csv`) e a
  rota tratada.

## Segurança do painel

- O servidor escuta só em `127.0.0.1` e recusa requisições de outros endereços e origens.
- Só existem as ações da tabela acima, e as rotas só podem ser escolhidas entre os arquivos
  listados.
- **Acesso remoto opcional, só de visualização:** com o `.env` preenchido (modelo em
  `.env.example`), aparelhos da mesma rede Tailscale também veem o painel, mas o servidor recusa
  qualquer comando que não venha do próprio notebook.

## Testes

```bash
docker run --rm --network none vida-painel:humble bash -lc \
  'source /opt/ros/humble/setup.bash && source /vida_ws/install/setup.bash && cd /painel && python3 -m pytest -q tests'
```

Para testar sem o carro, use o simulador do alto nível num domínio ROS separado:

```bash
docker run -d --rm --name vida_sim --network host -e ROS_DOMAIN_ID=77 -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  vida-ros-ws:humble bash -lc \
  'source /opt/ros/humble/setup.bash && source /vida_ws/install/setup.bash && exec ros2 launch vida_navigation simulation.launch.py'
docker run -d --rm --name vida_painel_teste --network host -e ROS_DOMAIN_ID=77 -e FASTDDS_BUILTIN_TRANSPORTS=UDPv4 \
  -e VIDA_PAINEL_PORT=18080 -e VIDA_PAINEL_DRY_RUN=1 -e VIDA_PAINEL_DATA=/tmp/d \
  -v ~/projeto-vida/analises:/caminhos:ro vida-painel:humble
```

O painel de teste fica em http://localhost:18080, sem executar comandos reais (`VIDA_PAINEL_DRY_RUN=1`).

## Licença

MIT. Veja [`LICENSE`](LICENSE).

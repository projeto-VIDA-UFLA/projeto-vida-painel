"""inhibit_flags do SafetyManager (firmware), com o nome para o operador."""

REARM = "Desligue e religue o enable físico para rearmar."

FLAGS = {
    1: ("enable físico desligado", "Ligue o enable físico para liberar o movimento.", False),
    2: ("enable lógico não recebido", "Confira o nó security no Odroid.", False),
    4: ("enable lógico falso", "Confira o gamepad e o nó security.", False),
    8: ("enable lógico atrasado", "Confira a comunicação com o ESP32.", False),
    16: ("referência de tração atrasada", "Confira se o carro foi iniciado.", False),
    32: ("referência de esterçamento atrasada", "Confira se o carro foi iniciado.", False),
    64: ("task de sensores parada", "Confira o ESP32.", False),
    128: ("leitura do LVDT inválida", "Confira o sensor de esterçamento.", False),
    256: ("saída do controlador atrasada", "Confira o ESP32.", False),
    512: ("esterçamento travado", REARM, True),
    1024: ("esterçamento no sentido errado", REARM, True),
    4096: ("encoders discordantes", REARM, True),
    8192: ("task travada", "Reinicie o ESP32.", True),
}


def decode(mask):
    """Lista de (bit, nome, o que fazer, retida); bits desconhecidos também aparecem."""
    result = []
    remaining = int(mask)
    for bit, (name, hint, retained) in FLAGS.items():
        if remaining & bit:
            result.append((bit, name, hint, retained))
            remaining &= ~bit
    bit = 1
    while remaining:
        if remaining & bit:
            result.append((bit, "flag desconhecida", "Confira o SafetyManager.", False))
            remaining &= ~bit
        bit <<= 1
    return result

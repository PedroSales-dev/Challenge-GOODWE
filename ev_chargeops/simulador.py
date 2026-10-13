"""
Simulador de eventos de recarga — substitui o carregador físico e a API GoodWe SEMS
em ambiente de desenvolvimento/prototipagem.

Gera 90 dias de sessões realistas para 10 unidades e 3 pontos de recarga,
incluindo 5 sessões anomalas injetadas deliberadamente para validar a Aura.

Perfis de uso gerados:
  - Pesado  (3-4 sessões/semana, 9–14 kWh por sessão)
  - Médio   (2-3 sessões/semana, 6–10 kWh por sessão)
  - Leve    (1-2 sessões/semana, 3–7 kWh por sessão)
"""
import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ev_chargeops.models import (
    Unidade, Usuario, CartaoRFID, PontoRecarga, SessaoRecarga, EventoTelemetria
)

random.seed(42)  # reprodutibilidade

# ---------------------------------------------------------------------------
# Parâmetros do condomínio simulado
# ---------------------------------------------------------------------------

UNIDADES_CONFIG = [
    # (identificacao, fracao_ideal, perfil, nome_usuario)
    ("Ap. 101", 0.130, "pesado",  "Carlos Mendes"),
    ("Ap. 102", 0.115, "pesado",  "Ana Souza"),
    ("Ap. 103", 0.110, "medio",   "Ricardo Lima"),
    ("Ap. 104", 0.105, "medio",   "Fernanda Costa"),
    ("Ap. 105", 0.100, "medio",   "João Alves"),
    ("Ap. 106", 0.095, "leve",    "Mariana Rocha"),
    ("Ap. 107", 0.090, "leve",    "Paulo Faria"),
    ("Ap. 108", 0.085, "leve",    "Beatriz Nunes"),
    ("Ap. 109", 0.085, "leve",    "Thiago Castro"),
    ("Ap. 110", 0.085, "leve",    "Luciana Pires"),
]

PERFIL_CONFIG = {
    "pesado": {"sessoes_semana": (3, 4), "kwh_range": (9.0, 14.0)},
    "medio":  {"sessoes_semana": (2, 3), "kwh_range": (6.0, 10.0)},
    "leve":   {"sessoes_semana": (1, 2), "kwh_range": (3.0,  7.0)},
}

# Horários preferidos por perfil (hora de início)
HORARIOS = {
    "pesado": list(range(18, 23)),    # noite
    "medio":  list(range(17, 22)),    # final de tarde / noite
    "leve":   list(range(8, 22)),     # qualquer hora do dia
}

DATA_INICIO = datetime(2026, 4, 1)
DIAS = 90
POTENCIA_CARREGADOR_KW = 11  # HCA G2 instalado


def _proxima_segunda(data: datetime) -> datetime:
    dias_ate_segunda = (7 - data.weekday()) % 7
    return data + timedelta(days=dias_ate_segunda)


def popular_banco(db: Session) -> dict:
    """
    Cria todas as entidades e retorna um dict com os IDs gerados,
    útil para referência no notebook.
    """
    # -----------------------------------------------------------------------
    # 1. Pontos de recarga
    # -----------------------------------------------------------------------
    pontos = []
    for i in range(1, 4):
        p = PontoRecarga(
            modelo_equipamento="GoodWe HCA G2",
            potencia_nominal_kw=POTENCIA_CARREGADOR_KW,
            protocolo_conectividade="OCPP 1.6",
            localizacao=f"Estacionamento L1 — Vaga {i}",
        )
        db.add(p)
        pontos.append(p)
    db.flush()

    # -----------------------------------------------------------------------
    # 2. Unidades, usuários e cartões RFID
    # -----------------------------------------------------------------------
    unidades = []
    usuarios = []
    cartoes = []
    uid_counter = 1000

    for identificacao, fracao, perfil, nome in UNIDADES_CONFIG:
        u = Unidade(identificacao=identificacao, fracao_ideal=fracao)
        db.add(u)
        db.flush()

        usr = Usuario(unidade_id=u.id, nome=nome, papel="morador")
        db.add(usr)
        db.flush()

        rfid = CartaoRFID(
            usuario_id=usr.id,
            uid_fisico=f"RFID{uid_counter:04d}",
            status="ativo",
        )
        db.add(rfid)
        db.flush()

        unidades.append((u, perfil))
        usuarios.append(usr)
        cartoes.append(rfid)
        uid_counter += 1

    # -----------------------------------------------------------------------
    # 3. Sessões de recarga (90 dias)
    # -----------------------------------------------------------------------
    sessoes_geradas = []
    kwh_acumulado_ponto = {p.id: 5000.0 for p in pontos}  # leitura inicial do medidor

    data_atual = DATA_INICIO
    ponto_idx = 0  # round-robin entre pontos

    for dia in range(DIAS):
        data_atual = DATA_INICIO + timedelta(days=dia)
        dia_semana = data_atual.weekday()  # 0=seg … 6=dom
        é_fds = dia_semana >= 5

        for idx, (unidade, perfil) in enumerate(unidades):
            cfg = PERFIL_CONFIG[perfil]
            sessoes_por_semana = random.randint(*cfg["sessoes_semana"])

            # probabilidade de carregar hoje
            prob_diaria = sessoes_por_semana / 7.0
            if é_fds:
                prob_diaria *= 1.3  # mais recarga no fim de semana

            if random.random() > prob_diaria:
                continue  # sem recarga hoje para este apartamento

            # atribui ponto em round-robin (simula fila na garagem)
            ponto = pontos[ponto_idx % len(pontos)]
            ponto_idx += 1

            kwh_sessao = round(random.uniform(*cfg["kwh_range"]), 2)
            duracao_h = kwh_sessao / (POTENCIA_CARREGADOR_KW * random.uniform(0.7, 1.0))
            hora_inicio = random.choice(HORARIOS[perfil])
            minuto_inicio = random.randint(0, 59)

            inicio = data_atual.replace(hour=hora_inicio, minute=minuto_inicio, second=0)
            fim = inicio + timedelta(hours=duracao_h)

            kwh_ini = round(kwh_acumulado_ponto[ponto.id], 3)
            kwh_fim = round(kwh_ini + kwh_sessao, 3)
            kwh_acumulado_ponto[ponto.id] = kwh_fim

            sessao = SessaoRecarga(
                usuario_id=usuarios[idx].id,
                cartao_id=cartoes[idx].id,
                ponto_id=ponto.id,
                inicio=inicio,
                fim=fim,
                kwh_inicial=kwh_ini,
                kwh_final=kwh_fim,
                status_encerramento="concluida_normal",
            )
            db.add(sessao)
            db.flush()

            # Telemetria a cada 15 minutos
            n_eventos = max(1, int(duracao_h * 4))
            for t in range(n_eventos):
                ts = inicio + timedelta(minutes=15 * t)
                potencia = POTENCIA_CARREGADOR_KW * random.uniform(0.85, 1.0)
                kwh_parcial = round(kwh_ini + (kwh_sessao * (t / max(1, n_eventos))), 3)
                evento = EventoTelemetria(
                    sessao_id=sessao.id,
                    timestamp=ts,
                    potencia_kw=round(potencia, 2),
                    energia_acumulada_kwh=kwh_parcial,
                )
                db.add(evento)

            sessoes_geradas.append(sessao)

    db.flush()

    # -----------------------------------------------------------------------
    # 4. Sessões anômalas injetadas
    # -----------------------------------------------------------------------
    _injetar_anomalias(db, usuarios, cartoes, pontos, kwh_acumulado_ponto)

    db.commit()

    return {
        "unidades": [u for u, _ in unidades],
        "usuarios": usuarios,
        "cartoes": cartoes,
        "pontos": pontos,
        "total_sessoes": len(sessoes_geradas) + 5,
    }


def _injetar_anomalias(db, usuarios, cartoes, pontos, kwh_acumulado_ponto):
    """Injeta 5 sessões com padrões anômalos para validar a detecção."""
    base = datetime(2026, 6, 15)
    ponto = pontos[0]

    anomalias_config = [
        # (usuario_idx, kwh, duracao_h, descricao)
        (2,  42.8,  4.0,  "consumo_muito_alto"),        # kWh absurdo para o perfil
        (6,  0.4,   8.5,  "duracao_alta_kwh_baixo"),    # plugado por muito tempo, nada carregou
        (4,  31.5,  0.5,  "potencia_instantanea_alta"), # kWh alto em tempo curto — potência impossível
        (1,  18.9,  9.2,  "duracao_acima_do_padrao"),   # muito tempo plugado
        (7,  38.2,  3.8,  "consumo_muito_alto"),        # segundo caso de consumo anormal
    ]

    for i, (usr_idx, kwh, dur_h, _tipo) in enumerate(anomalias_config):
        dia_offset = i * 2
        inicio = (base + timedelta(days=dia_offset)).replace(hour=22, minute=0, second=0)
        fim = inicio + timedelta(hours=dur_h)
        kwh_ini = round(kwh_acumulado_ponto[ponto.id], 3)
        kwh_fim = round(kwh_ini + kwh, 3)
        kwh_acumulado_ponto[ponto.id] = kwh_fim

        s = SessaoRecarga(
            usuario_id=usuarios[usr_idx].id,
            cartao_id=cartoes[usr_idx].id,
            ponto_id=ponto.id,
            inicio=inicio,
            fim=fim,
            kwh_inicial=kwh_ini,
            kwh_final=kwh_fim,
            status_encerramento="concluida_normal",
        )
        db.add(s)
    db.flush()

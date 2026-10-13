"""
Aura — Detecção de Anomalias.

Técnica: Isolation Forest (sklearn) sobre quatro features por sessão:
  - kwh_consumido      : energia entregue
  - duracao_horas      : tempo de conexão
  - potencia_media_kw  : kWh / duração (proxy de comportamento do carregador)
  - hora_inicio        : hora do dia (padrão de uso)

Sessões com score abaixo do limiar são persistidas na tabela Anomalia.
O classificador é retreinado a cada chamada com o histórico disponível,
o que é adequado para o volume de dados condominial (centenas de sessões/mês).
"""
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler
from sqlalchemy.orm import Session

from ev_chargeops.models import SessaoRecarga, Anomalia


CONTAMINATION = 0.04        # fração esperada de anomalias no histórico (~4%)
RANDOM_STATE = 42
LIMIAR_SCORE = -0.10        # decision_function: valores < limiar → anômalo


def _sessoes_para_dataframe(sessoes: list[SessaoRecarga]) -> pd.DataFrame:
    registros = []
    for s in sessoes:
        if s.kwh_final is None or s.fim is None:
            continue
        registros.append(
            {
                "id": s.id,
                "usuario_id": s.usuario_id,
                "kwh_consumido": s.kwh_consumido,
                "duracao_horas": s.duracao_horas,
                "potencia_media_kw": s.potencia_media_kw,
                "hora_inicio": s.inicio.hour,
            }
        )
    return pd.DataFrame(registros)


def detectar_e_persistir(
    db: Session,
    sessoes: list[SessaoRecarga],
    limpar_anteriores: bool = False,
) -> pd.DataFrame:
    """
    Treina o Isolation Forest, detecta anomalias e as persiste no banco.

    Parâmetros
    ----------
    db               : sessão SQLAlchemy aberta
    sessoes          : lista de SessaoRecarga a analisar
    limpar_anteriores: se True, remove anomalias previamente detectadas para essas sessões

    Retorna
    -------
    DataFrame com as sessões anômalas (colunas: id, tipo, score, features).
    """
    df = _sessoes_para_dataframe(sessoes)
    if df.empty or len(df) < 10:
        return pd.DataFrame()

    features = ["kwh_consumido", "duracao_horas", "potencia_media_kw", "hora_inicio"]
    X = df[features].values

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    clf = IsolationForest(
        contamination=CONTAMINATION,
        random_state=RANDOM_STATE,
        n_estimators=200,
    )
    clf.fit(X_scaled)

    scores = clf.decision_function(X_scaled)   # negativo = mais anômalo
    predicoes = clf.predict(X_scaled)          # -1 = anômalo, 1 = normal

    df["score_raw"] = scores
    df["anomalo"] = predicoes == -1

    anomalos = df[df["anomalo"]].copy()
    if anomalos.empty:
        return pd.DataFrame()

    # Normaliza score para 0–1 (legibilidade)
    s_min, s_max = scores.min(), scores.max()
    def normalizar(s):
        if s_max == s_min:
            return 0.5
        return round(1 - (s - s_min) / (s_max - s_min), 4)

    anomalos["score_normalizado"] = anomalos["score_raw"].apply(normalizar)

    # Classifica o tipo de anomalia com regras simples sobre as features
    def classificar(row):
        if row["kwh_consumido"] > 30:
            return "consumo_muito_alto"
        if row["duracao_horas"] > 8 and row["kwh_consumido"] < 5:
            return "duracao_alta_kwh_baixo"
        if row["potencia_media_kw"] > 13:
            return "potencia_instantanea_alta"
        if row["duracao_horas"] > 7:
            return "duracao_acima_do_padrao"
        return "padrao_fora_do_historico"

    anomalos["tipo"] = anomalos.apply(classificar, axis=1)

    # Persiste no banco
    sessoes_ids = set(s.id for s in sessoes)
    if limpar_anteriores:
        db.query(Anomalia).filter(Anomalia.sessao_id.in_(sessoes_ids)).delete(
            synchronize_session=False
        )
        db.flush()

    ids_ja_persistidos = set(
        row[0]
        for row in db.query(Anomalia.sessao_id)
        .filter(Anomalia.sessao_id.in_(sessoes_ids))
        .all()
    )

    for _, row in anomalos.iterrows():
        if row["id"] in ids_ja_persistidos:
            continue
        anomalia = Anomalia(
            sessao_id=row["id"],
            tipo=row["tipo"],
            score=row["score_normalizado"],
            status_revisao="pendente",
        )
        db.add(anomalia)

    db.commit()

    return anomalos[["id", "tipo", "score_normalizado", "kwh_consumido",
                      "duracao_horas", "potencia_media_kw", "hora_inicio"]].reset_index(drop=True)

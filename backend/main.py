"""
EV ChargeOps API — backend FastAPI.

Execução local:
    uvicorn backend.main:app --reload --port 8000

Documentação interativa:
    http://localhost:8000/docs
"""
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ev_chargeops.database import get_db, create_tables
from ev_chargeops import models
from ev_chargeops.rateio import (
    calcular_todas_as_faturas,
    resumo_faturamento,
    TARIFA_KWH_PADRAO,
    FATOR_PERDA_INTERNA,
    CUSTO_MANUTENCAO_MENSAL,
)
from ev_chargeops.ia.anomalias import detectar_e_persistir as detectar_anomalias
from ev_chargeops.ia.previsao import prever_demanda, metricas_historicas
from ev_chargeops.ia.boletim import gerar_boletim

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="EV ChargeOps API",
    description=(
        "Backend do protótipo EV ChargeOps — "
        "FIAP Enterprise Challenge 2026, GoodWe Energy Innovation Lab."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    create_tables()


# ---------------------------------------------------------------------------
# Schemas Pydantic
# ---------------------------------------------------------------------------
class SessaoOut(BaseModel):
    id: str
    usuario_id: str
    ponto_id: str
    inicio: datetime
    fim: Optional[datetime]
    kwh_consumido: float
    status_encerramento: str

    class Config:
        from_attributes = True


class FaturaOut(BaseModel):
    id: str
    unidade_id: str
    mes_referencia: str
    componente_variavel: float
    componente_fixo: float
    total: float
    gerada_em: datetime

    class Config:
        from_attributes = True


class IniciarSessaoIn(BaseModel):
    uid_rfid: str = Field(..., description="UID físico do cartão RFID")
    ponto_id: str = Field(..., description="ID do ponto de recarga")
    kwh_inicial: float = Field(..., description="Leitura atual do medidor (kWh)")


class EncerrarSessaoIn(BaseModel):
    kwh_final: float = Field(..., description="Leitura final do medidor (kWh)")


# ---------------------------------------------------------------------------
# Sessões
# ---------------------------------------------------------------------------
@app.post("/sessoes/iniciar", response_model=SessaoOut, tags=["Sessões"])
def iniciar_sessao(body: IniciarSessaoIn, db: Session = Depends(get_db)):
    """
    Inicia uma sessão de recarga ao receber um evento StartTransaction
    (autenticação RFID validada).
    """
    cartao = db.query(models.CartaoRFID).filter(
        models.CartaoRFID.uid_fisico == body.uid_rfid,
        models.CartaoRFID.status == "ativo",
    ).first()
    if not cartao:
        raise HTTPException(status_code=404, detail="Cartão RFID não encontrado ou bloqueado.")

    ponto = db.query(models.PontoRecarga).filter(
        models.PontoRecarga.id == body.ponto_id
    ).first()
    if not ponto:
        raise HTTPException(status_code=404, detail="Ponto de recarga não encontrado.")

    sessao = models.SessaoRecarga(
        usuario_id=cartao.usuario_id,
        cartao_id=cartao.id,
        ponto_id=ponto.id,
        inicio=datetime.utcnow(),
        kwh_inicial=body.kwh_inicial,
        status_encerramento="em_andamento",
    )
    db.add(sessao)
    db.commit()
    db.refresh(sessao)

    return SessaoOut(
        id=sessao.id,
        usuario_id=sessao.usuario_id,
        ponto_id=sessao.ponto_id,
        inicio=sessao.inicio,
        fim=sessao.fim,
        kwh_consumido=sessao.kwh_consumido,
        status_encerramento=sessao.status_encerramento,
    )


@app.post("/sessoes/{sessao_id}/encerrar", response_model=SessaoOut, tags=["Sessões"])
def encerrar_sessao(
    sessao_id: str, body: EncerrarSessaoIn, db: Session = Depends(get_db)
):
    """Encerra uma sessão ao receber StopTransaction."""
    sessao = db.query(models.SessaoRecarga).filter(
        models.SessaoRecarga.id == sessao_id,
        models.SessaoRecarga.status_encerramento == "em_andamento",
    ).first()
    if not sessao:
        raise HTTPException(status_code=404, detail="Sessão não encontrada ou já encerrada.")

    sessao.fim = datetime.utcnow()
    sessao.kwh_final = body.kwh_final
    sessao.status_encerramento = "concluida_normal"
    db.commit()
    db.refresh(sessao)

    return SessaoOut(
        id=sessao.id,
        usuario_id=sessao.usuario_id,
        ponto_id=sessao.ponto_id,
        inicio=sessao.inicio,
        fim=sessao.fim,
        kwh_consumido=sessao.kwh_consumido,
        status_encerramento=sessao.status_encerramento,
    )


@app.get("/sessoes", response_model=list[SessaoOut], tags=["Sessões"])
def listar_sessoes(
    mes: Optional[str] = None,
    unidade_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Lista sessões, com filtros opcionais por mês (YYYY-MM) e unidade."""
    q = db.query(models.SessaoRecarga)
    if mes:
        from sqlalchemy import extract
        ano, m = int(mes[:4]), int(mes[5:])
        q = q.filter(
            extract("year", models.SessaoRecarga.inicio) == ano,
            extract("month", models.SessaoRecarga.inicio) == m,
        )
    if unidade_id:
        q = q.join(models.SessaoRecarga.usuario).filter(
            models.Usuario.unidade_id == unidade_id
        )
    sessoes = q.order_by(models.SessaoRecarga.inicio.desc()).all()
    return [
        SessaoOut(
            id=s.id,
            usuario_id=s.usuario_id,
            ponto_id=s.ponto_id,
            inicio=s.inicio,
            fim=s.fim,
            kwh_consumido=s.kwh_consumido,
            status_encerramento=s.status_encerramento,
        )
        for s in sessoes
    ]


# ---------------------------------------------------------------------------
# Faturamento
# ---------------------------------------------------------------------------
@app.post("/faturas/calcular/{mes}", tags=["Faturamento"])
def calcular_faturas(mes: str, db: Session = Depends(get_db)):
    """Calcula e persiste as faturas de todas as unidades para o mês indicado."""
    faturas = calcular_todas_as_faturas(db, mes)
    resumo = resumo_faturamento(db, mes)
    return {"faturas_geradas": len(faturas), "resumo": resumo}


@app.get("/faturas/{mes}", tags=["Faturamento"])
def listar_faturas(mes: str, db: Session = Depends(get_db)):
    """Retorna todas as faturas geradas para o mês."""
    faturas = (
        db.query(models.Fatura)
        .filter(models.Fatura.mes_referencia == mes)
        .order_by(models.Fatura.total.desc())
        .all()
    )
    return [
        {
            "unidade": f.unidade.identificacao,
            "componente_variavel": f.componente_variavel,
            "componente_fixo": f.componente_fixo,
            "total": f.total,
            "n_sessoes": len(f.itens),
        }
        for f in faturas
    ]


# ---------------------------------------------------------------------------
# IA — Aura
# ---------------------------------------------------------------------------
@app.post("/ia/anomalias/{mes}", tags=["Aura — IA"])
def executar_deteccao_anomalias(mes: str, db: Session = Depends(get_db)):
    """Executa a detecção de anomalias sobre as sessões do mês."""
    from sqlalchemy import extract
    ano, m = int(mes[:4]), int(mes[5:])
    sessoes = (
        db.query(models.SessaoRecarga)
        .filter(
            extract("year", models.SessaoRecarga.inicio) == ano,
            extract("month", models.SessaoRecarga.inicio) == m,
            models.SessaoRecarga.kwh_final.isnot(None),
        )
        .all()
    )
    resultado = detectar_anomalias(db, sessoes)
    if resultado.empty:
        return {"anomalias": [], "total": 0}
    return {"anomalias": resultado.to_dict(orient="records"), "total": len(resultado)}


@app.get("/ia/previsao", tags=["Aura — IA"])
def executar_previsao(horizonte: int = 30, db: Session = Depends(get_db)):
    """Projeta o consumo diário dos próximos `horizonte` dias."""
    sessoes = db.query(models.SessaoRecarga).filter(
        models.SessaoRecarga.kwh_final.isnot(None)
    ).all()
    df = prever_demanda(db, sessoes, horizonte_dias=horizonte)
    if df.empty:
        return {"previsao": [], "horizonte_dias": horizonte}
    return {"previsao": df.to_dict(orient="records"), "horizonte_dias": horizonte}


@app.get("/ia/boletim/{mes}", tags=["Aura — IA"])
def gerar_boletim_mes(mes: str, db: Session = Depends(get_db)):
    """Gera e retorna o Boletim Energético Inteligente do mês."""
    from sqlalchemy import extract
    from ev_chargeops.models import Anomalia

    resumo = resumo_faturamento(db, mes)
    if not resumo:
        raise HTTPException(status_code=404, detail="Nenhuma fatura encontrada para o mês.")

    ano, m = int(mes[:4]), int(mes[5:])
    sessoes = db.query(models.SessaoRecarga).filter(
        models.SessaoRecarga.kwh_final.isnot(None)
    ).all()

    anomalias_persistidas = (
        db.query(models.Anomalia)
        .join(models.SessaoRecarga)
        .filter(
            extract("year", models.SessaoRecarga.inicio) == ano,
            extract("month", models.SessaoRecarga.inicio) == m,
        )
        .all()
    )

    import pandas as pd
    anomalias_df = pd.DataFrame(
        [{"id": a.sessao_id, "tipo": a.tipo, "score_normalizado": a.score}
         for a in anomalias_persistidas]
    )

    previsao_df = prever_demanda(db, sessoes, horizonte_dias=30)
    metricas = metricas_historicas(sessoes)

    texto = gerar_boletim(mes, resumo, anomalias_df, previsao_df, metricas)
    return {"mes": mes, "boletim": texto}

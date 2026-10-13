"""
Motor RHPS — Rateio Híbrido Proporcional por Sessão.

Fórmula:
    Fatura(unidade, mês) = Componente_Variável + Componente_Fixo

    Componente_Variável = Σ(kWh_sessão) × tarifa × (1 + fator_perda)
    Componente_Fixo     = custo_manutencao × fracao_ideal

Casos excepcionais tratados:
    1. Sessão interrompida — usa kWh parcial do último MeterValues, marcada com flag.
    2. Unidade sem recarga — variável = 0, fixo cobrado normalmente.
    3. Múltiplos cartões/veículos por unidade — consumo somado por unidade, não por cartão.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import extract, func

from ev_chargeops.models import (
    Unidade, SessaoRecarga, Fatura, ItemFatura
)


# ---------------------------------------------------------------------------
# Parâmetros tarifários (configuráveis por instalação)
# ---------------------------------------------------------------------------
TARIFA_KWH_PADRAO = 0.6878          # R$/kWh — tarifa residencial ENEL SP (jun/2026, bandeira amarela)
FATOR_PERDA_INTERNA = 0.03          # 3% — perdas técnicas na instalação interna
CUSTO_MANUTENCAO_MENSAL = 500.00    # R$ — custo fixo mensal da infraestrutura de 3 pontos


def calcular_fatura_unidade(
    db: Session,
    unidade_id: str,
    mes_referencia: str,                    # formato "YYYY-MM"
    tarifa: float = TARIFA_KWH_PADRAO,
    fator_perda: float = FATOR_PERDA_INTERNA,
    custo_manutencao: float = CUSTO_MANUTENCAO_MENSAL,
) -> Optional[Fatura]:
    """
    Calcula e persiste a fatura de uma unidade para um mês de referência.
    Retorna None se a unidade não existir.
    """
    unidade = db.query(Unidade).filter(Unidade.id == unidade_id).first()
    if unidade is None:
        return None

    ano, mes = int(mes_referencia[:4]), int(mes_referencia[5:7])

    # Todas as sessões concluídas (normal ou interrompida) da unidade no mês
    sessoes = (
        db.query(SessaoRecarga)
        .join(SessaoRecarga.usuario)
        .filter(
            SessaoRecarga.usuario.has(unidade_id=unidade_id),
            extract("year", SessaoRecarga.inicio) == ano,
            extract("month", SessaoRecarga.inicio) == mes,
            SessaoRecarga.status_encerramento.in_(["concluida_normal", "interrompida"]),
            SessaoRecarga.kwh_final.isnot(None),
        )
        .all()
    )

    # Componente variável — soma de todos os kWh das sessões
    itens = []
    kwh_total = 0.0
    for sessao in sessoes:
        kwh = sessao.kwh_consumido  # propriedade calculada no model
        custo_sessao = kwh * tarifa * (1.0 + fator_perda)
        itens.append(
            ItemFatura(
                sessao_id=sessao.id,
                kwh_atribuido=round(kwh, 4),
                valor=round(custo_sessao, 4),
            )
        )
        kwh_total += kwh

    componente_variavel = kwh_total * tarifa * (1.0 + fator_perda)
    componente_fixo = custo_manutencao * unidade.fracao_ideal
    total = componente_variavel + componente_fixo

    # Remove fatura anterior do mesmo mês se existir (recálculo)
    db.query(Fatura).filter(
        Fatura.unidade_id == unidade_id,
        Fatura.mes_referencia == mes_referencia,
    ).delete()
    db.flush()

    fatura = Fatura(
        unidade_id=unidade_id,
        mes_referencia=mes_referencia,
        componente_variavel=round(componente_variavel, 2),
        componente_fixo=round(componente_fixo, 2),
        total=round(total, 2),
    )
    db.add(fatura)
    db.flush()

    for item in itens:
        item.fatura_id = fatura.id
        db.add(item)

    db.commit()
    db.refresh(fatura)
    return fatura


def calcular_todas_as_faturas(
    db: Session,
    mes_referencia: str,
    tarifa: float = TARIFA_KWH_PADRAO,
    fator_perda: float = FATOR_PERDA_INTERNA,
    custo_manutencao: float = CUSTO_MANUTENCAO_MENSAL,
) -> list[Fatura]:
    """
    Calcula as faturas de todas as unidades cadastradas para o mês de referência.
    Retorna a lista ordenada por unidade.
    """
    unidades = db.query(Unidade).order_by(Unidade.identificacao).all()
    faturas = []
    for unidade in unidades:
        f = calcular_fatura_unidade(
            db, unidade.id, mes_referencia, tarifa, fator_perda, custo_manutencao
        )
        if f:
            faturas.append(f)
    return faturas


def resumo_faturamento(db: Session, mes_referencia: str) -> dict:
    """
    Retorna um dicionário com o resumo do faturamento do mês:
    total geral, kwh total, unidade de maior consumo, etc.
    """
    faturas = (
        db.query(Fatura)
        .filter(Fatura.mes_referencia == mes_referencia)
        .all()
    )
    if not faturas:
        return {}

    kwh_por_unidade = {}
    for f in faturas:
        kwh = sum(item.kwh_atribuido for item in f.itens)
        kwh_por_unidade[f.unidade.identificacao] = kwh

    maior_consumidora = max(kwh_por_unidade, key=kwh_por_unidade.get)

    return {
        "mes_referencia": mes_referencia,
        "total_unidades_faturadas": len(faturas),
        "kwh_total": round(sum(kwh_por_unidade.values()), 2),
        "total_componente_variavel": round(sum(f.componente_variavel for f in faturas), 2),
        "total_componente_fixo": round(sum(f.componente_fixo for f in faturas), 2),
        "total_geral": round(sum(f.total for f in faturas), 2),
        "maior_consumidora": maior_consumidora,
        "kwh_maior_consumidora": round(kwh_por_unidade[maior_consumidora], 2),
        "tarifa_utilizada": TARIFA_KWH_PADRAO,
    }

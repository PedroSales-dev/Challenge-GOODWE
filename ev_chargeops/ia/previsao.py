"""
Aura — Previsão de Demanda.

Técnica: Exponential Smoothing (Holt-Winters) via statsmodels.
Agrega o consumo diário total da instalação e projeta os próximos N dias.

Holt-Winters foi escolhido sobre ARIMA porque:
  - Captura sazonalidade semanal (demanda maior em finais de semana) naturalmente
    via componente sazonal com período 7.
  - Requer menos dados históricos para convergir do que SARIMA.
  - Tempo de treino adequado ao volume condominial.
"""
import warnings
import numpy as np
import pandas as pd
from sqlalchemy.orm import Session
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from ev_chargeops.models import SessaoRecarga


def _consumo_diario(sessoes: list[SessaoRecarga]) -> pd.Series:
    """Agrega kWh consumido por dia para toda a instalação."""
    if not sessoes:
        return pd.Series(dtype=float)

    registros = [
        {"data": s.inicio.date(), "kwh": s.kwh_consumido}
        for s in sessoes
        if s.kwh_final is not None
    ]
    df = pd.DataFrame(registros)
    if df.empty:
        return pd.Series(dtype=float)

    diario = df.groupby("data")["kwh"].sum()
    # Preenche dias sem recarga com zero (instalação ociosa)
    idx_completo = pd.date_range(diario.index.min(), diario.index.max(), freq="D")
    diario = diario.reindex(idx_completo, fill_value=0.0)
    return diario


def prever_demanda(
    db: Session,
    sessoes: list[SessaoRecarga],
    horizonte_dias: int = 30,
) -> pd.DataFrame:
    """
    Treina o modelo Holt-Winters e projeta o consumo diário para os próximos
    `horizonte_dias` dias.

    Retorna um DataFrame com colunas:
        data, kwh_previsto, intervalo_inferior, intervalo_superior
    """
    serie = _consumo_diario(sessoes)

    if len(serie) < 21:          # mínimo para sazonalidade semanal
        return pd.DataFrame()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        modelo = ExponentialSmoothing(
            serie,
            trend="add",
            seasonal="add",
            seasonal_periods=7,     # sazonalidade semanal
            initialization_method="estimated",
        )
        resultado = modelo.fit(optimized=True, use_brute=False)

    forecast = resultado.forecast(horizonte_dias)

    # Intervalo de confiança aproximado via desvio padrão dos resíduos
    residuos = resultado.resid.dropna()
    sigma = residuos.std() if not residuos.empty else 0.0

    df_forecast = pd.DataFrame(
        {
            "data": forecast.index,
            "kwh_previsto": forecast.values.clip(min=0),
            "intervalo_inferior": (forecast.values - 1.96 * sigma).clip(min=0),
            "intervalo_superior": forecast.values + 1.96 * sigma,
        }
    ).reset_index(drop=True)

    df_forecast["kwh_previsto"] = df_forecast["kwh_previsto"].round(2)
    df_forecast["intervalo_inferior"] = df_forecast["intervalo_inferior"].round(2)
    df_forecast["intervalo_superior"] = df_forecast["intervalo_superior"].round(2)

    return df_forecast


def metricas_historicas(sessoes: list[SessaoRecarga]) -> dict:
    """
    Estatísticas descritivas do histórico de consumo — usadas pelo boletim.
    """
    serie = _consumo_diario(sessoes)
    if serie.empty:
        return {}

    # Mapa dia da semana → consumo médio
    df = serie.reset_index()
    df.columns = ["data", "kwh"]
    df["dia_semana"] = pd.to_datetime(df["data"]).dt.day_name(locale=None)
    por_dia = df.groupby("dia_semana")["kwh"].mean().round(2).to_dict()

    return {
        "kwh_medio_diario": round(serie.mean(), 2),
        "kwh_max_diario": round(serie.max(), 2),
        "kwh_min_diario": round(serie.min(), 2),
        "kwh_total_historico": round(serie.sum(), 2),
        "dias_com_recarga": int((serie > 0).sum()),
        "dias_sem_recarga": int((serie == 0).sum()),
        "consumo_por_dia_semana": por_dia,
    }

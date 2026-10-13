"""
Aura — Boletim Energético Inteligente.

Gera um relatório mensal em linguagem natural a partir de dados estruturados.
A abordagem usa templates parametrizados (sem LLM externo), garantindo
disponibilidade e reprodutibilidade no ambiente de prototipagem.

Na Sprint 02 seguinte, o template pode ser substituído por uma chamada a LLM
(ex: API Anthropic) para geração mais fluida — a interface da função permanece
idêntica, apenas a engine muda.
"""
from datetime import date, datetime
from typing import Optional

import pandas as pd


MESES_PT = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril",
    5: "maio", 6: "junho", 7: "julho", 8: "agosto",
    9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


def gerar_boletim(
    mes_referencia: str,            # "YYYY-MM"
    resumo_rateio: dict,
    anomalias_df: pd.DataFrame,
    previsao_df: pd.DataFrame,
    metricas_hist: dict,
    nome_instalacao: str = "Energy Innovation Lab — FIAP/GoodWe",
) -> str:
    """
    Gera o boletim como string Markdown.

    Parâmetros
    ----------
    mes_referencia  : mês do boletim ("YYYY-MM")
    resumo_rateio   : saída de rateio.resumo_faturamento()
    anomalias_df    : saída de anomalias.detectar_e_persistir()
    previsao_df     : saída de previsao.prever_demanda()
    metricas_hist   : saída de previsao.metricas_historicas()
    nome_instalacao : nome da instalação para o cabeçalho
    """
    ano, mes = int(mes_referencia[:4]), int(mes_referencia[5:7])
    mes_nome = MESES_PT[mes].capitalize()
    hoje = datetime.utcnow().strftime("%d/%m/%Y às %H:%Mh UTC")

    n_anomalias = len(anomalias_df) if not anomalias_df.empty else 0
    total_unidades = resumo_rateio.get("total_unidades_faturadas", 0)
    kwh_total = resumo_rateio.get("kwh_total", 0)
    total_geral = resumo_rateio.get("total_geral", 0)
    maior_consumidora = resumo_rateio.get("maior_consumidora", "—")
    kwh_maior = resumo_rateio.get("kwh_maior_consumidora", 0)
    tarifa = resumo_rateio.get("tarifa_utilizada", 0)
    kwh_medio = metricas_hist.get("kwh_medio_diario", 0)

    # Previsão próximo mês
    kwh_previsto_prox = 0
    if not previsao_df.empty:
        kwh_previsto_prox = round(previsao_df["kwh_previsto"].sum(), 1)

    # Contexto de anomalias
    contexto_anomalias = _texto_anomalias(n_anomalias)

    # Recomendação de horário
    recomendacao = _recomendacao_horario(metricas_hist)

    boletim = f"""# Boletim Energético Inteligente — {mes_nome}/{ano}
## {nome_instalacao}
*Gerado automaticamente por Aura (EV ChargeOps) em {hoje}*

---

## Resumo do Mês

Em {mes_nome.lower()} de {ano}, a instalação registrou **{kwh_total:.1f} kWh** de energia
entregue a veículos elétricos, distribuídos entre **{total_unidades} unidades** ativas.
O consumo médio diário foi de **{kwh_medio:.1f} kWh/dia**.

O custo total do mês somou **R$ {total_geral:.2f}**, calculado pela tarifa de
R$ {tarifa:.4f}/kWh com 3% de fator de perda interna, acrescido do rateio fixo
de manutenção da infraestrutura proporcional à fração ideal de cada unidade.

A unidade com maior consumo foi **{maior_consumidora}**, com **{kwh_maior:.1f} kWh**
registrados no período.

---

## Qualidade dos Dados

{contexto_anomalias}

---

## Projeção para o Próximo Mês

Com base no histórico dos últimos 90 dias e no modelo Holt-Winters com
sazonalidade semanal, a projeção de consumo total para o próximo mês é de
**{kwh_previsto_prox:.1f} kWh**{_comparacao_tendencia(kwh_total, kwh_previsto_prox)}.

{recomendacao}

---

## Orientações ao Gestor

- As faturas individuais já estão disponíveis no painel para consulta e repasse.
- Sessões marcadas como anômalas **não foram excluídas do faturamento** — o consumo
  medido foi mantido. Revise manualmente e ajuste se necessário antes do fechamento.
- Atualize a tarifa de referência sempre que a distribuidora publicar novo valor ou
  alterar a bandeira tarifária.

---
*EV ChargeOps v0.1.0 — Protótipo Sprint 02 — FIAP Enterprise Challenge 2026*
"""
    return boletim.strip()


def _texto_anomalias(n: int) -> str:
    if n == 0:
        return (
            "Nenhuma anomalia foi detectada no período. "
            "Todos os registros de sessão estão dentro do padrão histórico."
        )
    if n == 1:
        return (
            "**1 sessão anômala** foi identificada pelo módulo de detecção da Aura. "
            "A sessão foi marcada com status `pendente` e está disponível para revisão manual "
            "no painel do gestor. O custo foi mantido no faturamento até que a revisão "
            "confirme ou descarte a anomalia."
        )
    return (
        f"**{n} sessões anômalas** foram identificadas pelo módulo de detecção da Aura. "
        f"Os padrões identificados incluem consumo acima do histórico, duração "
        f"atípica e potência instantânea inconsistente. Todas estão com status "
        f"`pendente` e disponíveis para revisão no painel do gestor."
    )


def _comparacao_tendencia(atual: float, previsto: float) -> str:
    if atual == 0:
        return ""
    variacao = ((previsto - atual) / atual) * 100
    if abs(variacao) < 3:
        return ", mantendo o patamar atual"
    if variacao > 0:
        return f", representando um aumento de {variacao:.1f}% em relação ao mês atual"
    return f", representando uma redução de {abs(variacao):.1f}% em relação ao mês atual"


def _recomendacao_horario(metricas: dict) -> str:
    consumo_por_dia = metricas.get("consumo_por_dia_semana", {})
    if not consumo_por_dia:
        return ""

    mais_alto = max(consumo_por_dia, key=consumo_por_dia.get, default=None)
    mais_baixo = min(consumo_por_dia, key=consumo_por_dia.get, default=None)

    if mais_alto is None:
        return ""

    return (
        f"O dia de maior demanda histórica é **{mais_alto}** "
        f"({consumo_por_dia[mais_alto]:.1f} kWh médio). "
        f"O dia de menor demanda é **{mais_baixo}** "
        f"({consumo_por_dia[mais_baixo]:.1f} kWh médio). "
        f"Sugerimos comunicar aos moradores que concentrem recargas nos dias "
        f"de menor demanda para evitar picos na demanda contratada."
    )

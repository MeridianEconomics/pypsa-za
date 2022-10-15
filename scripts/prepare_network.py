# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: : 2017-2020 The PyPSA-Eur Authors, 2021 PyPSA-Africa Authors
#
# SPDX-License-Identifier: MIT
# coding: utf-8
"""
Prepare PyPSA network for solving according to :ref:`opts` and :ref:`ll`, such as

- adding an annual **limit** of carbon-dioxide emissions,
- adding an exogenous **price** per tonne emissions of carbon-dioxide (or other kinds),
- setting an **N-1 security margin** factor for transmission line capacities,
- specifying an expansion limit on the **cost** of transmission expansion,
- specifying an expansion limit on the **volume** of transmission expansion, and
- reducing the **temporal** resolution by averaging over multiple hours
  or segmenting time series into chunks of varying lengths using ``tsam``.

Relevant Settings
-----------------

.. code:: yaml

    costs:
        emission_prices:
        USD2013_to_EUR2013:
        discountrate:
        marginal_cost:
        capital_cost:

    electricity:
        co2limit:
        max_hours:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at
    :ref:`costs_cf`, :ref:`electricity_cf`

Inputs
------

- ``data/costs.csv``: The database of cost assumptions for all included technologies for specific years from various sources; e.g. discount rate, lifetime, investment (CAPEX), fixed operation and maintenance (FOM), variable operation and maintenance (VOM), fuel costs, efficiency, carbon-dioxide intensity.
- ``networks/elec_s{simpl}_{clusters}.nc``: confer :ref:`cluster`

Outputs
-------

- ``networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc``: Complete PyPSA network that will be handed to the ``solve_network`` rule.

Description
-----------

.. tip::
    The rule :mod:`prepare_all_networks` runs
    for all ``scenario`` s in the configuration file
    the rule :mod:`prepare_network`.

"""
import logging
import os
import re

import numpy as np
import pandas as pd
import pypsa
from _helpers import configure_logging
from add_electricity import load_costs, update_transmission_costs
from temporal_clustering import prepare_timeseries_tsam, tsam_clustering, cluster_snapshots

idx = pd.IndexSlice

logger = logging.getLogger(__name__)


def add_wind_and_solar_limits(n):
    capacity_per_sqm = snakemake.config['respotentials']['capacity_per_sqm']
    onwind_area = pd.read_csv(snakemake.input.onwind_area, index_col=0).loc[lambda s: s.available_area > 0.]['available_area']
    solar_area = pd.read_csv(snakemake.input.solar_area, index_col=0).loc[lambda s: s.available_area > 0.]['available_area']
    onwind_max_capacity = onwind_area * capacity_per_sqm['onwind']
    solar_max_capacity  = solar_area * capacity_per_sqm['solar']

    p_nom_max_limit = n.generators.p_nom_max.groupby([n.generators.carrier, n.generators.bus]).max()
    p_nom_max_limit['onwind'] = onwind_max_capacity
    p_nom_max_limit['solar'] = solar_max_capacity

    # global p_nom_max for each carrier + investment_period at each node
    p_nom_max_inv_p = pd.DataFrame(np.repeat([p_nom_max_limit.values],
                                            len(n.snapshots.levels[0]), axis=0),
                                index=n.snapshots.levels[0], columns=p_nom_max_limit.index)

    logger.info("Set maximum installed capacity")
    for carrier in ["onwind","solar"]:
        nodes = p_nom_max_inv_p[carrier].columns
        max_cap = p_nom_max_inv_p[carrier].iloc[0,:].rename(lambda x: "TechLimit " + x + " " +carrier)
        n.madd("GlobalConstraint",
              "TechLimit " + nodes + " " + carrier,
              carrier_attribute=carrier,
              sense="<=",
              type="tech_capacity_expansion_limit",
              bus=nodes,
              constant=max_cap)

# def add_co2limit(n, factor=None):

#     if factor is not None:
#         co2_emissions_limit = factor * snakemake.config["electricity"]["co2base"]
#     else:
#         co2_emissions_limit = snakemake.config["electricity"]["co2limit"]

#     n.add(
#         "GlobalConstraint",
#         "CO2Limit",
#         carrier_attribute="co2_emissions",
#         sense="<=",
#         constant=co2_emissions_limit,
#     )

def add_co2limit(n):
    n.add("GlobalConstraint", "CO2Limit",
          carrier_attribute="co2_emissions", 
          sense="<=",
          constant=snakemake.config['electricity']['co2limit'])


    # n.add("GlobalConstraint",
    #       "CO2neutral",
    #       type="primary_energy",
    #       carrier_attribute="co2_emissions",
    #       investment_period=n.snapshots.levels[0][-1],
    #       sense="<=",
    #       constant=0)


def add_gaslimit(n, gaslimit):

    sel = n.carriers.index.intersection(["OCGT", "CCGT", "CHP"])
    n.carriers.loc[sel, "gas_usage"] = 1.0

    n.add(
        "GlobalConstraint",
        "GasLimit",
        carrier_attribute="gas_usage",
        sense="<=",
        constant=gaslimit,
    )



def add_emission_prices(n, emission_prices={"co2": 0.0}, exclude_co2=False):
    if exclude_co2:
        emission_prices.pop("co2")
    ep = (
        pd.Series(emission_prices).rename(lambda x: x + "_emissions")
        * n.carriers.filter(like="_emissions")
    ).sum(axis=1)
    gen_ep = n.generators.carrier.map(ep) / n.generators.efficiency
    n.generators["marginal_cost"] += gen_ep
    su_ep = n.storage_units.carrier.map(ep) / n.storage_units.efficiency_dispatch
    n.storage_units["marginal_cost"] += su_ep


def set_line_s_max_pu(n):
    s_max_pu = snakemake.config["lines"]["s_max_pu"]
    n.lines["s_max_pu"] = s_max_pu
    logger.info(f"N-1 security margin of lines set to {s_max_pu}")


def set_transmission_limit(n, ll_type, factor, costs, Nyears=1):
    links_dc_b = n.links.carrier == "DC" if not n.links.empty else pd.Series()

    _lines_s_nom = (
        np.sqrt(3)
        * n.lines.type.map(n.line_types.i_nom)
        * n.lines.num_parallel
        * n.lines.bus0.map(n.buses.v_nom)
    )
    lines_s_nom = n.lines.s_nom.where(n.lines.type == "", _lines_s_nom)

    col = "capital_cost" if ll_type == "c" else "length"
    ref = (
        lines_s_nom @ n.lines[col]
        + n.links.loc[links_dc_b, "p_nom"] @ n.links.loc[links_dc_b, col]
    )

    update_transmission_costs(n, costs)

    if factor == "opt" or float(factor) > 1.0:
        n.lines["s_nom_min"] = lines_s_nom
        n.lines["s_nom_extendable"] = True

        n.links.loc[links_dc_b, "p_nom_min"] = n.links.loc[links_dc_b, "p_nom"]
        n.links.loc[links_dc_b, "p_nom_extendable"] = True

    if factor != "opt":
        con_type = "expansion_cost" if ll_type == "c" else "volume_expansion"
        rhs = float(factor) * ref
        n.add(
            "GlobalConstraint",
            f"l{ll_type}_limit",
            type=f"transmission_{con_type}_limit",
            sense="<=",
            constant=rhs,
            carrier_attribute="AC, DC",
        )
    return n


def average_every_nhours(n, offset):
    logger.info(f"Resampling the network to {offset}")
    m = n.copy()#with_time=False)

    if len(n.investment_periods)>1:
        snapshots_unstacked = n.snapshots.get_level_values(1)
    else:
        snapshots_unstacked = n.snapshots.copy()

    snapshot_weightings = n.snapshot_weightings.copy().set_index(snapshots_unstacked).resample(offset).sum()
    snapshot_weightings=snapshot_weightings[snapshot_weightings.index.year.isin(n.investment_periods)]
    snapshot_weightings.index = pd.MultiIndex.from_arrays([snapshot_weightings.index.year, snapshot_weightings.index])
    m.set_snapshots(snapshot_weightings.index)
    m.snapshot_weightings = snapshot_weightings

    for c in n.iterate_components():
        pnl = getattr(m, c.list_name + "_t")
        for k, df in c.pnl.items():
            if not df.empty:
                resampled = df.set_index(snapshots_unstacked).resample(offset).mean()
                resampled=resampled[resampled.index.year.isin(n.investment_periods)]
                resampled.index = snapshot_weightings.index
                pnl[k] = resampled
    return m

def apply_time_segmentation(n, segments, config):

    n = cluster_snapshots(n, normed=False, noTypicalPeriods=30)
        # n, 
        #             normed=config['normed'], 
        #             noTypicalPeriods=segments, 
        #             extremePeriodMethod = config['extremePeriodMethod'],
        #             rescaleClusterPeriods= config['rescaleClusterPeriods'], 
        #             hoursPerPeriod=int(config['hoursPerPeriod']),
        #             clusterMethod=config['clusterMethod'],
        #             solver='cbc',
        #             predefClusterOrder=None)
    return n

# def apply_time_segmentation(n, segments, solver_name):
#     logger.info(f"Aggregating time series to {segments} segments.")
#     try:
#         import tsam.timeseriesaggregation as tsam
#     except:
#         raise ModuleNotFoundError(
#             "Optional dependency 'tsam' not found." "Install via 'pip install tsam'"
#         )

#     p_max_pu_norm = n.generators_t.p_max_pu.max()
#     p_max_pu = n.generators_t.p_max_pu / p_max_pu_norm

#     load_norm = n.loads_t.p_set.max()
#     load = n.loads_t.p_set / load_norm

#     inflow_norm = n.storage_units_t.inflow.max()
#     inflow = n.storage_units_t.inflow / inflow_norm

#     raw = pd.concat([p_max_pu, load, inflow], axis=1, sort=False)

#     agg = tsam.TimeSeriesAggregation(
#         raw,
#         hoursPerPeriod=len(raw),
#         noTypicalPeriods=1,
#         noSegments=int(segments),
#         segmentation=True,
#         solver=solver_name,
#     )

#     segmented = agg.createTypicalPeriods()

#     weightings = segmented.index.get_level_values("Segment Duration")
#     offsets = np.insert(np.cumsum(weightings[:-1]), 0, 0)
#     snapshots = [n.snapshots[0] + pd.Timedelta(f"{offset}h") for offset in offsets]

#     n.set_snapshots(pd.DatetimeIndex(snapshots, name="name"))
#     n.snapshot_weightings = pd.Series(
#         weightings, index=snapshots, name="weightings", dtype="float64"
#     )

#     segmented.index = snapshots
#     n.generators_t.p_max_pu = segmented[n.generators_t.p_max_pu.columns] * p_max_pu_norm
#     n.loads_t.p_set = segmented[n.loads_t.p_set.columns] * load_norm
#     n.storage_units_t.inflow = segmented[n.storage_units_t.inflow.columns] * inflow_norm

#     return n

def set_line_nom_max(n, s_nom_max_set=np.inf, p_nom_max_set=np.inf):
    n.lines.s_nom_max.clip(upper=s_nom_max_set, inplace=True)
    n.links.p_nom_max.clip(upper=p_nom_max_set, inplace=True)


if __name__ == "__main__":
    if 'snakemake' not in globals():
        from _helpers import mock_snakemake
        snakemake = mock_snakemake('prepare_network', **{'costs':'ambitions',
                            'regions':'RSA',
                            'resarea':'redz',
                            'll':'copt',
                            'opts':'LC',
                            'attr':'p_nom'})
    configure_logging(snakemake)

    opts = snakemake.wildcards.opts.split("-")

    n = pypsa.Network(snakemake.input[0])
    Nyears = n.snapshot_weightings.objective.sum() / 8760.0
    costs = load_costs(
        snakemake.input.tech_costs,
        snakemake.wildcards.costs,
        snakemake.config["costs"],
        snakemake.config["electricity"],
        snakemake.config["years"],
    )

    add_wind_and_solar_limits(n)
    set_line_s_max_pu(n)

    for o in opts:
        m = re.match(r"^\d+h$", o, re.IGNORECASE)
        if m is not None:
            n = average_every_nhours(n, m.group(0))
            break

    for o in opts:
        m = re.match(r"^\d+seg$", o, re.IGNORECASE)
        if m is not None:
            n = apply_time_segmentation(n, m.group(0)[:-3],snakemake.config["tsam_clustering"])
            break

    for o in opts:
        if "Co2L" in o:
            m = re.findall("[0-9]*\.?[0-9]+$", o)
            if len(m) > 0:
                co2limit = float(m[0]) * snakemake.config["electricity"]["co2base"]
                add_co2limit(n)
                logger.info("Setting CO2 limit according to wildcard value.")
            else:
                add_co2limit(n)
                logger.info("Setting CO2 limit according to config value.")
            break

    for o in opts:
        if "CH4L" in o:
            m = re.findall("[0-9]*\.?[0-9]+$", o)
            if len(m) > 0:
                limit = float(m[0]) * 1e6
                add_gaslimit(n, limit, Nyears)
                logger.info("Setting gas usage limit according to wildcard value.")
            else:
                add_gaslimit(n, snakemake.config["electricity"].get("gaslimit"), Nyears)
                logger.info("Setting gas usage limit according to config value.")

        for o in opts:
            oo = o.split("+")
            suptechs = map(lambda c: c.split("-", 2)[0], n.carriers.index)
            if oo[0].startswith(tuple(suptechs)):
                carrier = oo[0]
                # handles only p_nom_max as stores and lines have no potentials
                attr_lookup = {
                    "p": "p_nom_max",
                    "c": "capital_cost",
                    "m": "marginal_cost",
                }
                attr = attr_lookup[oo[1][0]]
                factor = float(oo[1][1:])
                if carrier == "AC":  # lines do not have carrier
                    n.lines[attr] *= factor
                else:
                    comps = {"Generator", "Link", "StorageUnit", "Store"}
                    for c in n.iterate_components(comps):
                        sel = c.df.carrier.str.contains(carrier)
                        c.df.loc[sel, attr] *= factor

        for o in opts:
            if "Ep" in o:
                m = re.findall("[0-9]*\.?[0-9]+$", o)
                if len(m) > 0:
                    logger.info("Setting emission prices according to wildcard value.")
                    add_emission_prices(n, dict(co2=float(m[0])))
                else:
                    logger.info("Setting emission prices according to config value.")
                    add_emission_prices(n, snakemake.config["costs"]["emission_prices"])
                break

    ll_type, factor = snakemake.wildcards.ll[0], snakemake.wildcards.ll[1:]
    set_transmission_limit(n, ll_type, factor, costs, Nyears)

    set_line_nom_max(
        n,
        s_nom_max_set=snakemake.config["lines"].get("s_nom_max,", np.inf),
        p_nom_max_set=snakemake.config["links"].get("p_nom_max,", np.inf),
    )

    n.export_to_netcdf(snakemake.output[0])

# coding: utf-8
import logging
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import powerplantmatching as pm
import pypsa
import xarray as xr
from _helpers import configure_logging, getContinent, update_p_nom_max, pdbcast
from shapely.validation import make_valid
from shapely.geometry import Point
from vresutils import transfer as vtransfer

idx = pd.IndexSlice

logger = logging.getLogger(__name__)


# import networkx as nx
# import pandas as pd
# import numpy as np
# import scipy as sp
# from operator import attrgetter
# from six import string_types

# import rasterio
# import fiona
# import rasterstats
# import geopandas as gpd

# from shapely.geometry import Point
# from vresutils.shapes import haversine
# from vresutils.costdata import annuity

# import pypsa

# from _helpers import pdbcast

def normed(s):
    return s / s.sum()

def calculate_annuity(n, r):
    """
    Calculate the annuity factor for an asset with lifetime n years and
    discount rate of r, e.g. annuity(20, 0.05) * 20 = 1.6
    """
    if isinstance(r, pd.Series):
        return pd.Series(1 / n, index=r.index).where(
            r == 0, r / (1.0 - 1.0 / (1.0 + r) ** n)
        )
    elif r > 0:
        return r / (1.0 - 1.0 / (1.0 + r) ** n)
    else:
        return 1 / n

def _add_missing_carriers_from_costs(n, costs, carriers):
    missing_carriers = pd.Index(carriers).difference(n.carriers.index)
    if missing_carriers.empty:
        return

    emissions_cols = (
        costs.columns.to_series().loc[lambda s: s.str.endswith("_emissions")].values
    )
    suptechs = missing_carriers.str.split("-").str[0]
    emissions = costs.loc[suptechs, emissions_cols].fillna(0.0)
    emissions.index = missing_carriers
    n.import_components_from_dataframe(emissions, "Carrier")

def load_costs(tech_costs, config, elec_config, Nyears=1):
    """
    set all asset costs and other parameters
    """
    costs = pd.read_csv(tech_costs, index_col=list(range(3))).sort_index()

    # correct units to MW and EUR
    costs.loc[costs.unit.str.contains("/kW"), "value"] *= 1e3
    costs.loc[costs.unit.str.contains("USD"), "value"] *= config["USD2013_to_EUR2013"]
    costs.loc[costs.unit.str.contains("EUR"), "value"] *= config["EUR2013_to_ZAR2013"]

    costs = (
        costs.loc[idx[:, config["year"], :], "value"]
        .unstack(level=2)
        .groupby("technology")
        .sum(min_count=1)
    )
    costs['efficiency_store']=costs['efficiency'].pow(1./2) #if only 1 efficiency value is given assume it is round trip efficiency
    costs['efficiency_dispatch']=costs['efficiency'].pow(1./2)
    costs = costs.fillna(
        {
            "CO2 intensity": 0,
            "FOM": 0,
            "VOM": 0,
            "discount rate": config["discountrate"],
            "efficiency": 1,
            "efficiency_store": 1,
            "efficiency_dispatch": 1,
            "fuel": 0,
            "investment": 0,
            "lifetime": 25,
        }
    )

    costs["capital_cost"] = (
        (
            calculate_annuity(costs["lifetime"], costs["discount rate"])
            + costs["FOM"] / 100.0
        )
        * costs["investment"]
        * Nyears
    )

    costs.at["OCGT", "fuel"] = costs.at["gas", "fuel"]
    costs.at["CCGT", "fuel"] = costs.at["gas", "fuel"]

    costs["marginal_cost"] = costs["VOM"] + costs["fuel"] / costs["efficiency"]

    costs = costs.rename(columns={"CO2 intensity": "co2_emissions"})

    costs.at["OCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]
    costs.at["CCGT", "co2_emissions"] = costs.at["gas", "co2_emissions"]

    costs.at["solar", "capital_cost"] = 0.5 * (
        costs.at["solar-rooftop", "capital_cost"]
        + costs.at["solar-utility", "capital_cost"]
    )

    def costs_for_storage(store, link1, link2=None, max_hours=1.0):
        capital_cost = link1["capital_cost"] + max_hours * store["capital_cost"]
        if link2 is not None:
            capital_cost += link2["capital_cost"]
        return pd.Series(
            dict(capital_cost=capital_cost, marginal_cost=0.0, co2_emissions=0.0)
        )

    max_hours = elec_config["max_hours"]
    costs.at["battery"] = costs_for_storage(
        costs.loc["battery storage"],
        costs.loc["battery inverter"],
        max_hours=max_hours["battery"],
    )
    costs.loc['battery',:].fillna(costs.loc['battery inverter',:],inplace=True)
    
    costs.at["H2"] = costs_for_storage(
        costs.loc["hydrogen storage"],
        costs.loc["fuel cell"],
        costs.loc["electrolysis"],
        max_hours=max_hours["H2"],
    )
    costs.loc['H2',:].fillna(costs.loc['electrolysis',:],inplace=True)
    costs.at['H2', 'efficiency_store'] = costs.at['electrolysis','efficiency']
    costs.at['H2', 'efficiency_dispatch'] = costs.at['fuel cell','efficiency']

    for attr in ("marginal_cost", "capital_cost"):
        overwrites = config.get(attr)
        if overwrites is not None:
            overwrites = pd.Series(overwrites)
            costs.loc[overwrites.index, attr] = overwrites

    return costs

# def load_costs():
#     costs = pd.read_excel(snakemake.input.tech_costs,
#                           sheet_name=snakemake.wildcards.cost,
#                           index_col=0).T

#     discountrate = snakemake.config['costs']['discountrate']
#     costs['capital_cost'] = ((annuity(costs.pop('Lifetime [a]'), discountrate) +
#                               costs.pop('FOM [%/a]').fillna(0.) / 100.)
#                              * costs.pop('Overnight cost [R/kW_el]')*1e3)

#     costs['efficiency'] = costs.pop('Efficiency').fillna(1.)
#     costs['marginal_cost'] = (costs.pop('VOM [R/MWh_el]').fillna(0.) +
#                               (costs.pop('Fuel cost [R/MWh_th]') / costs['efficiency']).fillna(0.))

#     emissions_cols = costs.columns.to_series().loc[lambda s: s.str.endswith(' emissions [kg/MWh_th]')]
#     costs.loc[:, emissions_cols.index] = (costs.loc[:, emissions_cols.index]/1e3).fillna(0.)
#     costs = costs.rename(columns=emissions_cols.str[:-len(" [kg/MWh_th]")].str.lower().str.replace(' ', '_'))

#     for attr in ('marginal_cost', 'capital_cost'):
#         overwrites = snakemake.config['costs'].get(attr)
#         if overwrites is not None:
#             overwrites = pd.Series(overwrites)
#             costs.loc[overwrites.index, attr] = overwrites

#     return costs

# ## Attach components

# ### Load

def attach_load(n):
    load = pd.read_csv(snakemake.input.load)
    load = load.set_index(
        pd.to_datetime(load['SETTLEMENT_DATE'] + ' ' +
                       load['PERIOD'].astype(str) + ':00')
        .rename('t')
    )['SYSTEMENERGY']

    demand = (snakemake.config['electricity']['demand'] *
              normed(load.loc[snakemake.config['historical_year']]))
    n.madd("Load", n.buses.index,
           bus=n.buses.index,
           p_set=pdbcast(demand, normed(n.buses.population)))

### Set line costs

# def update_transmission_costs(n, costs):
#     opts = snakemake.config['lines']
#     for df in (n.lines, n.links):
#         if df.empty: continue

#         df['capital_cost'] = (df['length'] / opts['s_nom_factor'] *
#                               costs.at['Transmission lines', 'capital_cost'])

def update_transmission_costs(n, costs, length_factor=1.0, simple_hvdc_costs=False):
    n.lines["capital_cost"] = (
        n.lines["length"] * length_factor * costs.at["HVAC overhead", "capital_cost"]
    )

    if n.links.empty:
        return

    dc_b = n.links.carrier == "DC"
    # If there are no "DC" links, then the 'underwater_fraction' column
    # may be missing. Therefore we have to return here.
    # TODO: Require fix
    if n.links.loc[n.links.carrier == "DC"].empty:
        return

    if simple_hvdc_costs:
        costs = (
            n.links.loc[dc_b, "length"]
            * length_factor
            * costs.at["HVDC overhead", "capital_cost"]
        )
    else:
        costs = (
            n.links.loc[dc_b, "length"]
            * length_factor
            * (
                (1.0 - n.links.loc[dc_b, "underwater_fraction"])
                * costs.at["HVDC overhead", "capital_cost"]
                + n.links.loc[dc_b, "underwater_fraction"]
                * costs.at["HVDC submarine", "capital_cost"]
            )
            + costs.at["HVDC inverter pair", "capital_cost"]
        )
    n.links.loc[dc_b, "capital_cost"] = costs


# ### Generators - TODO Update from pypa-eur
def attach_wind_and_solar(n, costs):
    historical_year = snakemake.config['historical_year']
    capacity_per_sqm = snakemake.config['respotentials']['capacity_per_sqm']

    ## Onshore wind
    n.add("Carrier", name="onwind")
    onwind_area = pd.read_csv(snakemake.input.onwind_area, index_col=0).loc[lambda s: s.available_area > 0.]
    onwind_res = (pd.read_excel(snakemake.input.onwind_profiles,
                             skiprows=[1], sheet_name='Wind power profiles')
                    .rename(columns={'supply area\'s name': 't'}).set_index('t')
                    .resample('1h').mean().loc[historical_year]
                    .reindex(columns=onwind_area.index)
                    .clip(lower=0., upper=1.))
    n.madd("Generator", onwind_area.index, suffix=" onwind",
           bus=onwind_area.index,
           carrier="onwind",
           p_nom_extendable=True,
           p_nom_max=onwind_area.available_area * capacity_per_sqm['onwind'],
           marginal_cost=costs.at['onwind', 'marginal_cost'],
           capital_cost=costs.at['onwind', 'capital_cost'],
           efficiency=costs.at['onwind', 'efficiency'],
           p_max_pu=onwind_res)

    ## Solar PV
    n.add("Carrier", name="solar")
    solar_area = pd.read_csv(snakemake.input.solar_area, index_col=0).loc[lambda s: s.available_area > 0.]
    solar_res = (pd.read_excel(snakemake.input.solar_profiles,
                           skiprows=[1], sheet_name='PV profiles')
             .rename(columns={'supply area\'s name': 't'})
             .set_index('t')
             .resample('1h').mean().loc[historical_year].reindex(n.snapshots, fill_value=0.)
             .reindex(columns=solar_area.index)
             .clip(lower=0., upper=1.))
    n.madd("Generator", solar_area.index, suffix=" solar",
           bus=solar_area.index,
           carrier="solar",
           p_nom_extendable=True,
           p_nom_max=solar_area.available_area * capacity_per_sqm['solar'],
           marginal_cost=costs.at['solar', 'marginal_cost'],
           capital_cost=costs.at['solar', 'capital_cost'],
           efficiency=costs.at['solar', 'efficiency'],
           p_max_pu=solar_res)


# # Generators
def attach_existing_generators(n, costs):
    historical_year = snakemake.config['historical_year']

    ps_f = dict(efficiency="Pump Efficiency (%)",
                pump_units="Pump Units",
                pump_load="Pump Load per unit (MW)",
                max_storage="Pumped Storage - Max Storage (GWh)")

    csp_f = dict(max_hours='CSP Storage (hours)')

    g_f = dict(fom="Fixed Operations and maintenance costs (R/kW/yr)",
               p_nom='Installed/ Operational Capacity in 2016 (MW)',
               name='Power Station Name',
               carrier='Fuel/technology type',
               decomdate='Decommissioning Date',
               x='GPS Longitude',
               y='GPS Latitude',
               status='Status',
               heat_rate='Heat Rate (GJ/MWh)',
               fuel_price='Fuel Price (R/GJ)',
               vom='Variable Operations and Maintenance Cost (R/MWh)',
               max_ramp_up='Max Ramp Up (MW/min)',
               unit_size='Unit size (MW)',
               units='Number units',
               maint_rate='Typical annual maintenance rate (%)',
               out_rate='Typical annual forced outage rate (%)',
               owner='Owner')

    gens = pd.read_excel(snakemake.input.existing_generators, na_values=['-'])

    # Make field "Fixed Operations and maintenance costs" numeric
    includescapex_i = gens[g_f['fom']].str.endswith(' (includes capex)').dropna().index
    gens.loc[includescapex_i, g_f['fom']] = gens.loc[includescapex_i, g_f['fom']].str[:-len(' (includes capex)')]
    gens[g_f['fom']] = pd.to_numeric(gens[g_f['fom']])


    # Calculate fields where pypsa uses different conventions
    gens['efficiency'] = 3.6/gens.pop(g_f['heat_rate'])
    gens['marginal_cost'] = 3.6*gens.pop(g_f['fuel_price'])/gens['efficiency'] + gens.pop(g_f['vom'])
    gens['capital_cost'] = 1e3*gens.pop(g_f['fom'])
    gens['ramp_limit_up'] = 60*gens.pop(g_f['max_ramp_up'])/gens[g_f['p_nom']]

    year = snakemake.config['year']
    print(year)
    gens = (gens
            # rename remaining fields
            .rename(columns={g_f[f]: f
                             for f in {'p_nom', 'name', 'carrier', 'x', 'y'}})
            # remove all power plants decommissioned before 2030
            .loc[lambda df: ((pd.to_datetime(df[g_f['decomdate']].replace({'beyond 2050': np.nan}).dropna()) >= year)
                                .reindex(df.index, fill_value=True))]
            # drop unused fields
            .drop([g_f[f] for f in {'unit_size', 'units', 'maint_rate',
                                    'out_rate', 'decomdate', 'status'}], axis=1)
    ).set_index('name')

    # CahoraBassa will be added later, even though we don't have coordinates
    CahoraBassa = gens.loc["CahoraBassa"]

    # Drop power plants where we don't have coordinates or capacity
    gens = pd.DataFrame(gens.loc[lambda df: (df.p_nom>0.) & df.x.notnull() & df.y.notnull()])

    # Associate every generator with the bus of the region it is in or closest to
    pos = gpd.GeoSeries([Point(o.x, o.y) for o in gens[['x', 'y']].itertuples()], index=gens.index)

    regions = gpd.read_file(snakemake.input.supply_regions).set_index('name')

    for bus, region in regions.geometry.iteritems():
        pos_at_bus_b = pos.within(region)
        if pos_at_bus_b.any():
            gens.loc[pos_at_bus_b, "bus"] = bus

    gens.loc[gens.bus.isnull(), "bus"] = pos[gens.bus.isnull()].map(lambda p: regions.distance(p).idxmin())

    if snakemake.wildcards.regions=='RSA':
        CahoraBassa['bus'] = "RSA"
    elif snakemake.wildcards.regions=='27-supply':
        CahoraBassa['bus'] = "POLOKWANE"
    gens = gens.append(CahoraBassa)

    # Now we split them by carrier and have some more carrier specific cleaning
    gens.carrier.replace({"Pumped Storage": "PHS"}, inplace=True)

    # HYDRO

    n.add("Carrier", "hydro")
    n.add("Carrier", "PHS")

    hydro = pd.DataFrame(gens.loc[gens.carrier.isin({'PHS', 'hydro'})])
    hydro["efficiency_store"] = hydro["efficiency_dispatch"] = np.sqrt(hydro.pop(ps_f['efficiency'])/100.).fillna(1.)

    hydro["max_hours"] = 1e3*hydro.pop(ps_f["max_storage"])/hydro["p_nom"]

    hydro["p_min_pu"] = - (hydro.pop(ps_f["pump_load"]) * hydro.pop(ps_f["pump_units"]) / hydro["p_nom"]).fillna(0.)

    hydro = (hydro
             .assign(p_max_pu=1.0, cyclic_state_of_charge=True)
             .drop(list(csp_f.values()) + ['ramp_limit_up', 'efficiency'], axis=1))

    hydro.max_hours.fillna(hydro.max_hours.mean(), inplace=True)

    hydro_inflow = pd.read_csv(snakemake.input.hydro_inflow, index_col=0, parse_dates=True).loc[historical_year]
    hydro_za_b = (hydro.index.to_series() != 'CahoraBassa')
    hydro_inflow_za = pd.DataFrame(hydro_inflow[['ZA']].values * normed(hydro.loc[hydro_za_b, 'p_nom'].values),
                                   columns=hydro.index[hydro_za_b], index=hydro_inflow.index)
    hydro_inflow_za['CahoraBassa'] = hydro.at['CahoraBassa', 'p_nom']/2187.*hydro_inflow['MZ']

    hydro.marginal_cost.fillna(0., inplace=True)
    n.import_components_from_dataframe(hydro, "StorageUnit")
    n.import_series_from_dataframe(hydro_inflow_za, "StorageUnit", "inflow")

    if snakemake.config['electricity'].get('csp'):
        n.add("Carrier", "CSP")

        csp = (pd.DataFrame(gens.loc[gens.carrier == "CSP"])
               .drop(list(ps_f.values()) + ["ramp_limit_up", "efficiency"], axis=1)
               .rename(columns={csp_f['max_hours']: 'max_hours'}))

        # TODO add to network with time-series and everything

    gens = (gens.loc[gens.carrier.isin({"coal", "nuclear"})]
            .drop(list(ps_f.values()) + list(csp_f.values()), axis=1))
    _add_missing_carriers_from_costs(n, costs, gens.carrier.unique())
    n.import_components_from_dataframe(gens, "Generator")

def attach_extendable_generators(n, costs):
    elec_opts = snakemake.config['electricity']
    carriers = elec_opts['extendable_carriers']['Generator']
    buses = elec_opts['buses'][snakemake.wildcards.regions]

    _add_missing_carriers_from_costs(n, costs, carriers)

    for carrier in carriers:
        buses_i = buses.get(carrier, n.buses.index)
        n.madd("Generator", buses_i, suffix=" " + carrier,
               bus=buses_i,
               p_nom_extendable=True,
               carrier=carrier,
               capital_cost=costs.at[carrier, 'capital_cost'],
               marginal_cost=costs.at[carrier, 'marginal_cost'],
               efficiency=costs.at[carrier, 'efficiency'])


def attach_storage(n, costs):
    elec_opts = snakemake.config['electricity']
    carriers = elec_opts['extendable_carriers']['StorageUnit']
    max_hours = elec_opts['max_hours']
    buses = elec_opts['buses']

    _add_missing_carriers_from_costs(n, costs, carriers)

    for carrier in carriers:
        buses_i = buses.get(carrier, n.buses.index)
        n.madd("StorageUnit", buses_i, " " + carrier,
               bus=buses_i,
               p_nom_extendable=True,
               carrier=carrier,
               capital_cost=costs.at[carrier, 'capital_cost'],
               marginal_cost=costs.at[carrier, 'marginal_cost'],
               efficiency_store=costs.at[carrier, 'efficiency_store'],
               efficiency_dispatch=costs.at[carrier, 'efficiency_dispatch'],
               max_hours=max_hours[carrier],
               cyclic_state_of_charge=True)

def add_co2limit(n):
    n.add("GlobalConstraint", "CO2Limit",
          carrier_attribute="co2_emissions", sense="<=",
          constant=snakemake.config['electricity']['co2limit'])

def add_emission_prices(n, emission_prices=None, exclude_co2=False):
    if emission_prices is None:
        emission_prices = snakemake.config['costs']['emission_prices']
    if exclude_co2: emission_prices.pop('co2')
    ep = (pd.Series(emission_prices).rename(lambda x: x+'_emissions') * n.carriers).sum(axis=1)
    n.generators['marginal_cost'] += n.generators.carrier.map(ep)
    n.storage_units['marginal_cost'] += n.storage_units.carrier.map(ep)

def add_peak_demand_hour_without_variable_feedin(n):
    new_hour = n.snapshots[-1] + pd.Timedelta(hours=1)
    n.set_snapshots(n.snapshots.append(pd.Index([new_hour])))

    # Don't value new hour for energy totals
    n.snapshot_weightings[new_hour] = 0.

    # Don't allow variable feed-in in this hour
    n.generators_t.p_max_pu.loc[new_hour] = 0.

    n.loads_t.p_set.loc[new_hour] = (
        n.loads_t.p_set.loc[n.loads_t.p_set.sum(axis=1).idxmax()]
        * (1.+snakemake.config['electricity']['SAFE_reservemargin'])
    )



if __name__ == "__main__":
    opts = snakemake.wildcards.opts.split('-')
    n = pypsa.Network(snakemake.input.base_network)
    Nyears = n.snapshot_weightings.objective.sum() / 8760.0
    costs = load_costs(
        snakemake.input.tech_costs,
        snakemake.config["costs"],
        snakemake.config["electricity"],
        Nyears,
    )
    attach_load(n)
    update_transmission_costs(n, costs)
    attach_existing_generators(n, costs)
    attach_wind_and_solar(n, costs)
    attach_extendable_generators(n, costs)
    attach_storage(n, costs)

    # if 'Co2L' in opts:
    #     add_co2limit(n)
    #     add_emission_prices(n, exclude_co2=True)

    # if 'Ep' in opts:
    #     add_emission_prices(n)

    # if 'SAFE' in opts:
    #     add_peak_demand_hour_without_variable_feedin(n)

    n.export_to_netcdf(snakemake.output[0])

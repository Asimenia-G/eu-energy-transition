"""Compare household electricity prices and renewable generation in six EU countries."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

COUNTRIES = {'EL': ('GRC', 'Greece'), 'DE': ('DEU', 'Germany'),
             'ES': ('ESP', 'Spain'), 'FR': ('FRA', 'France'),
             'IT': ('ITA', 'Italy'), 'PT': ('PRT', 'Portugal')}
START, END = 2015, 2025
EUROSTAT = 'https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nrg_pc_204'
EMBER = 'https://files.ember-energy.org/public-downloads/generation/outputs/release_generation_yearly_lower.csv'
TAXES = ['I_TAX', 'X_TAX']


def download(data_dir, refresh=False):
    """Cache the original source files so the analysis can be reproduced offline."""
    data_dir.mkdir(parents=True, exist_ok=True)
    params = {'lang': 'en', 'freq': 'S', 'siec': 'E7000',
              'nrg_cons': 'KWH2500-4999', 'unit': 'KWH', 'currency': 'EUR',
              'tax': TAXES, 'geo': list(COUNTRIES)}
    for name, url, query in [('eurostat.json', EUROSTAT, params),
                             ('ember.csv', EMBER, None)]:
        path = data_dir / name
        if path.exists() and not refresh:
            continue
        response = requests.get(url, params=query, timeout=120)
        response.raise_for_status()
        if name.endswith('.json'):
            if 'value' not in response.json():
                raise ValueError('Eurostat returned no observations; inspect the API response.')
        elif b'ISO 3 code' not in response.content[:4096]:
            raise ValueError('Unexpected Ember response; check the source URL.')
        path.write_bytes(response.content)
        path.with_suffix(path.suffix + '.metadata.json').write_text(json.dumps({
            'url': response.url, 'downloaded_utc': datetime.now(timezone.utc).isoformat(),
            'sha256': hashlib.sha256(response.content).hexdigest(),
            'last_modified': response.headers.get('Last-Modified')}, indent=2))


def decode_eurostat(payload):
    """Decode sparse JSON-stat values and observation flags into a tidy table."""
    dims, sizes = payload['id'], payload['size']
    labels = []
    for dim in dims:
        index = payload['dimension'][dim]['category']['index']
        labels.append(sorted(index, key=index.get) if isinstance(index, dict) else index)
    values, flags = payload['value'], payload.get('status', {})

    def get(container, i, default=None):
        if isinstance(container, dict):
            return container.get(str(i), default)
        return container[i] if i < len(container) else default

    rows = []
    for i, coordinate in enumerate(np.ndindex(*sizes)):
        row = dict(zip(dims, [labels[j][k] for j, k in enumerate(coordinate)]))
        row.update(price_eur_kwh=get(values, i), flag=get(flags, i, '') or '')
        rows.append(row)
    return pd.DataFrame(rows)


def clean_prices(payload):
    df = decode_eurostat(payload)
    for dim, code in {'freq': 'S', 'siec': 'E7000', 'nrg_cons': 'KWH2500-4999',
                      'unit': 'KWH', 'currency': 'EUR'}.items():
        if set(df[dim]) != {code}:
            raise ValueError(f'Unexpected Eurostat dimension: {dim}')
    df = df[df.geo.isin(COUNTRIES) & df.tax.isin(TAXES)].copy()
    df['year'] = df.time.str[:4].astype(int)
    df['semester'] = df.time.str[-2:]
    df = df[df.year.between(START, END)]
    if not df.semester.isin(['S1', 'S2']).all():
        raise ValueError('Unexpected semester code')
    df['iso3'] = df.geo.map({k: v[0] for k, v in COUNTRIES.items()})
    df['price_eur_kwh'] = pd.to_numeric(df.price_eur_kwh, errors='raise')
    if df.duplicated(['iso3', 'year', 'semester', 'tax']).any():
        raise ValueError('Duplicate Eurostat country/semester/tax observations')
    if not np.isfinite(df.price_eur_kwh.dropna()).all():
        raise ValueError('Non-finite price')
    # Negative reported retail prices are retained for inspection, not silently changed.
    return df[['iso3', 'year', 'semester', 'tax', 'price_eur_kwh', 'flag']]


def annual_prices(df):
    """Simple average of two half-year prices; no imputation or volume weighting."""
    annual = df.groupby(['iso3', 'year', 'tax']).price_eur_kwh.agg(['mean', 'count'])
    annual.loc[annual['count'] != 2, 'mean'] = np.nan
    prices = annual['mean'].unstack('tax').reindex(columns=TAXES)
    counts = annual['count'].unstack('tax').reindex(columns=TAXES)
    prices.columns = ['price_incl_tax', 'price_excl_tax']
    counts.columns = ['n_semesters_incl_tax', 'n_semesters_excl_tax']
    return prices.join(counts).reset_index()


def clean_generation(df):
    """Use Ember's reported aggregate renewable share, not a sum of fuel aggregates."""
    df = df.rename(columns={'ISO 3 code': 'iso3', 'Year': 'year',
                            'Electricity source': 'source',
                            'Share of generation (%)': 'renewables_pct'})
    required = {'iso3', 'year', 'source', 'renewables_pct'}
    if not required.issubset(df.columns):
        raise ValueError(f'Unexpected Ember schema; missing {required - set(df.columns)}')
    df['year'] = pd.to_numeric(df.year, errors='raise')
    df = df[df.iso3.isin([v[0] for v in COUNTRIES.values()]) &
            df.year.between(START, END) &
            df.source.astype(str).str.strip().str.casefold().eq('renewables')].copy()
    df['renewables_pct'] = pd.to_numeric(df.renewables_pct, errors='raise')
    if df.duplicated(['iso3', 'year']).any():
        raise ValueError('Duplicate Ember country-year renewable observations')
    if not df.renewables_pct.dropna().between(0, 100).all():
        raise ValueError('Renewable share outside 0–100%')
    if df.empty:
        raise ValueError('No matching Ember data; inspect the source file')
    return df[['iso3', 'year', 'renewables_pct']]


def combine(prices, generation):
    grid = pd.MultiIndex.from_product([[v[0] for v in COUNTRIES.values()],
                                      range(START, END + 1)],
                                     names=['iso3', 'year']).to_frame(index=False)
    df = grid.merge(prices, how='left', validate='one_to_one').merge(
        generation, how='left', validate='one_to_one')
    df.insert(1, 'country', df.iso3.map({v[0]: v[1] for v in COUNTRIES.values()}))
    df['tax_price_gap'] = df.price_incl_tax - df.price_excl_tax
    df['complete'] = df[['price_incl_tax', 'price_excl_tax', 'renewables_pct']].notna().all(axis=1)
    return df


def summarise(df):
    """Compare endpoints and descriptive within-country annual changes."""
    rows = []
    for country, group in df.groupby('country'):
        g = group.set_index('year').sort_index()
        changes = g[['price_incl_tax', 'renewables_pct']].diff().dropna()
        correlation = (changes.corr().iloc[0, 1] if len(changes) >= 3
                       and changes.nunique().min() > 1 else np.nan)
        rows.append({'country': country, 'complete_years': int(g.complete.sum()),
                     'price_2015': g.loc[START, 'price_incl_tax'],
                     'price_2025': g.loc[END, 'price_incl_tax'],
                     'price_change_eur_kwh': g.loc[END, 'price_incl_tax'] - g.loc[START, 'price_incl_tax'],
                     'renewables_change_pp': g.loc[END, 'renewables_pct'] - g.loc[START, 'renewables_pct'],
                     'n_paired_annual_changes': len(changes),
                     'correlation_annual_changes': correlation})
    return pd.DataFrame(rows)


# A consistent palette across the figures, with Greece highlighted.
COLORS = {'Greece': '#007C83', 'Germany': '#A66B27', 'Spain': '#5369A3',
          'France': '#A84F72', 'Italy': '#71658D', 'Portugal': '#577C52'}
INK = '#20343F'


def make_figure(title, subtitle, source, ylabel):
    fig, ax = plt.subplots(figsize=(10, 5.8), facecolor='white')
    fig.subplots_adjust(left=.12, right=.96, top=.74, bottom=.19)
    fig.text(.12, .94, title, fontsize=19, fontweight='bold', color=INK)
    fig.text(.12, .885, subtitle, fontsize=10.5, color='#52636D')
    fig.text(.12, .055, source, fontsize=9, color='#52636D')
    ax.set(ylabel=ylabel, xticks=range(START, END + 1, 2),
           xlim=(START - .25, END + .25))
    ax.set_axisbelow(True)
    ax.grid(axis='y', color='#DDE5E8', linewidth=.8)
    for spine in ['top', 'right', 'left']:
        ax.spines[spine].set_visible(False)
    ax.spines['bottom'].set_color('#C5D0D5')
    ax.tick_params(axis='both', length=0, labelsize=10, colors=INK, pad=7)
    ax.yaxis.label.set_color(INK)
    return fig, ax


def plot(df, out):
    for column, title, subtitle, ylabel, source, filename in [
        ('price_incl_tax', 'Household electricity prices',
         'Six European countries | 2015–2025 | Including taxes and levies',
         'Nominal EUR/kWh', 'Source: Eurostat (nrg_pc_204). Equal-weight mean of two semesters.',
         'household_prices.png'),
        ('renewables_pct', 'The rise of renewable electricity',
         'Six European countries | 2015–2025 | Share of domestic generation',
         'Renewable generation (%)', 'Source: Ember, Yearly Electricity Data.',
         'renewables.png')]:
        fig, ax = make_figure(title, subtitle, source, ylabel)
        for country, g in df.groupby('country', sort=False):
            g = g.sort_values('year')
            focus = country == 'Greece'
            ax.plot(g.year, g[column], color=COLORS[country], label=country,
                    marker='o', ms=4 if focus else 3, lw=2.8 if focus else 1.8,
                    zorder=3 if focus else 2)
        ax.legend(loc='lower left', bbox_to_anchor=(0, 1.025), ncol=6,
                  frameon=False, fontsize=9, handlelength=1.8, columnspacing=1.3,
                  borderaxespad=0)
        fig.savefig(out / filename, dpi=300)
        plt.close(fig)

    fig, ax = make_figure(
        'Greece: why the price definition matters',
        'Household electricity prices | 2015–2025 | Two tax definitions',
        'Source: Eurostat (nrg_pc_204). Equal-weight mean of two semesters.',
        'Nominal EUR/kWh')
    g = df[df.country == 'Greece'].sort_values('year')
    ax.plot(g.year, g.price_incl_tax, color=COLORS['Greece'], marker='o',
            ms=5, lw=2.5, label='Including taxes and levies')
    ax.plot(g.year, g.price_excl_tax, color='#C47B35', marker='o',
            ms=5, lw=2.5, label='Excluding taxes and levies')
    ax.fill_between(g.year, g.price_incl_tax, g.price_excl_tax,
                    where=g.price_excl_tax > g.price_incl_tax,
                    interpolate=True, color='#C47B35', alpha=.13)
    for row in g[g.tax_price_gap < 0].itertuples():
        ax.annotate(f'{row.year}: price reversal',
                    (row.year, row.price_excl_tax), xytext=(-14, -14),
                    textcoords='offset points', ha='right', va='top',
                    fontsize=9.5, color=INK,
                    arrowprops={'arrowstyle': '-', 'color': '#52636D', 'lw': .8})
    ax.legend(loc='lower left', bbox_to_anchor=(0, 1.025), ncol=2,
              frameon=False, fontsize=10, borderaxespad=0)
    fig.savefig(out / 'greece_tax_comparison.png', dpi=300)
    plt.close(fig)


def run(data_dir, out):
    half = clean_prices(json.loads((data_dir / 'eurostat.json').read_text()))
    generation = clean_generation(pd.read_csv(data_dir / 'ember.csv', low_memory=False))
    df = combine(annual_prices(half), generation)
    if not df.complete.any():
        raise ValueError('No complete country-years; check input data')
    out.mkdir(parents=True, exist_ok=True)
    half.to_csv(out / 'semester_prices.csv', index=False)
    df.to_csv(out / 'country_year.csv', index=False)
    df[~df.complete].to_csv(out / 'incomplete_country_years.csv', index=False)
    df[df.tax_price_gap < 0].to_csv(out / 'negative_tax_gaps.csv', index=False)
    summary = summarise(df)
    summary.to_csv(out / 'summary.csv', index=False)
    half[half.flag.ne('') | half.price_eur_kwh.isna() | half.price_eur_kwh.lt(0)].to_csv(
        out / 'price_quality_flags.csv', index=False)
    plot(df, out)
    print(summary.round(3).to_string(index=False))
    print(f'Complete country-years: {df.complete.sum()}/{len(df)}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data'))
    parser.add_argument('--out', type=Path, default=Path('results'))
    parser.add_argument('--refresh', action='store_true', help='Download fresh source files')
    parser.add_argument('--offline', action='store_true', help='Use existing source files only')
    args = parser.parse_args()
    if args.offline and args.refresh:
        parser.error('--offline and --refresh are mutually exclusive')
    if not args.offline:
        download(args.data, args.refresh)
    run(args.data, args.out)

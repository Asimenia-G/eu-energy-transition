"""Synthetic fixtures test the transformations; these are not energy observations."""
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from analysis import annual_prices, clean_prices, clean_generation, combine, run


def fixture():
    dims = {'freq': ['S'], 'siec': ['E7000'], 'nrg_cons': ['KWH2500-4999'],
            'unit': ['KWH'], 'currency': ['EUR'], 'tax': ['I_TAX', 'X_TAX'],
            'geo': ['EL'], 'time': ['2015-S1', '2015-S2', '2016-S1', '2016-S2']}
    return {'id': list(dims), 'size': [len(v) for v in dims.values()],
            'dimension': {k: {'category': {'index': {x: i for i, x in enumerate(v)}}}
                          for k, v in dims.items()},
            'value': {'0': .20, '1': .30, '2': .40, '4': .10, '5': .20, '6': .20, '7': .30},
            'status': {'1': 'p'}}


def generation_fixture():
    return pd.DataFrame({'ISO 3 code': ['GRC', 'GRC', 'GRC'], 'Year': [2015, 2016, 2015],
                         'Electricity source': ['Renewables', 'Renewables', 'Solar'],
                         'Share of generation (%)': [30, 35, 10]})


class TestAnalysis(unittest.TestCase):
    def test_prices(self):
        half = clean_prices(fixture())
        self.assertEqual(half.iloc[1].flag, 'p')
        annual = annual_prices(half).set_index('year')
        self.assertAlmostEqual(annual.loc[2015, 'price_incl_tax'], .25)
        self.assertTrue(pd.isna(annual.loc[2016, 'price_incl_tax']))
        self.assertEqual(annual.loc[2016, 'n_semesters_incl_tax'], 1)

    def test_generation(self):
        df = clean_generation(generation_fixture())
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0].renewables_pct, 30)
        with self.assertRaises(ValueError):
            clean_generation(pd.concat([generation_fixture(), generation_fixture()]))
        bad = generation_fixture()
        bad.loc[0, 'Share of generation (%)'] = 120
        with self.assertRaises(ValueError):
            clean_generation(bad)

    def test_merge(self):
        df = combine(annual_prices(clean_prices(fixture())), clean_generation(generation_fixture()))
        self.assertEqual(len(df), 66)
        self.assertEqual(df.complete.sum(), 1)
        self.assertAlmostEqual(df.loc[df.complete, 'tax_price_gap'].iloc[0], .1)

    def test_offline_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / 'eurostat.json').write_text(json.dumps(fixture()))
            generation_fixture().to_csv(path / 'ember.csv', index=False)
            run(path, path / 'results')
            for name in ['country_year.csv', 'summary.csv', 'price_quality_flags.csv',
                         'household_prices.png', 'renewables.png', 'greece_tax_comparison.png']:
                self.assertGreater((path / 'results' / name).stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()

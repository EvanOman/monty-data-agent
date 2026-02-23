from dataclasses import dataclass

BASE_URL = "https://calmcode.io/static/data"


@dataclass(frozen=True)
class Dataset:
    name: str
    filename: str
    fmt: str
    description: str
    rows_approx: int

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.filename}"


DATASETS: list[Dataset] = [
    Dataset(
        name="titanic",
        filename="titanic.csv",
        fmt="csv",
        description="Titanic passenger survival data (survived, pclass, name, sex, age, fare, sibsp, parch)",
        rows_approx=714,
    ),
    Dataset(
        name="bigmac",
        filename="bigmac.csv",
        fmt="csv",
        description="Big Mac Index economics data (date, currency_code, name, local_price, dollar_ex, dollar_price)",
        rows_approx=1330,
    ),
    Dataset(
        name="smoking",
        filename="smoking.csv",
        fmt="csv",
        description="Simpson's paradox dataset on smoking/survival (outcome, smoker, age)",
        rows_approx=1314,
    ),
    Dataset(
        name="stocks",
        filename="stocks.csv",
        fmt="csv",
        description="Stock prices for MSFT, KLM, ING, MOS (Date, MSFT, KLM, ING, MOS)",
        rows_approx=4276,
    ),
    Dataset(
        name="pokemon",
        filename="pokemon.json",
        fmt="json",
        description="Pokemon stats (name, type, total, hp, attack)",
        rows_approx=800,
    ),
    Dataset(
        name="stigler",
        filename="stigler.csv",
        fmt="csv",
        description="Stigler diet optimization data (commodity, unit, price_cents, calories, protein_g, ...nutrients)",
        rows_approx=77,
    ),
]

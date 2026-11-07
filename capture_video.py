"""Читает CSV и для каждой строки записывает видео через qscript.CaptureVideo_Magento."""
import csv

import qscript


def main():
    # utf-8-sig: Excel сохраняет UTF-8 с BOM, иначе первая колонка не совпала бы с "URL".
    with open("input.csv", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        for row in reader:
            path = qscript.CaptureVideo_Magento(
                url=row["URL"],
                company_name=row["Company Name"],
                language=row["Language"],
                country=row["Country"],
                postcode=row["Postcode"],
                city=row["City"],
                street=row["Street"],
                house_no=row["HouseNo"],
            )
            if path:
                print(path)


if __name__ == "__main__":
    main()

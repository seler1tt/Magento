"""Читает CSV и для каждой строки записывает видео через qscript.CaptureVideo_Magento.

С --server каждое записанное видео загружается на веб-сервис и открывается в Chromium.
"""
import argparse
import csv
import sys

import qscript
import video_client


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        help="URL веб-сервиса (например, http://127.0.0.1:8000); без него видео только записываются",
    )
    parser.add_argument(
        "--view-seconds",
        type=float,
        help="сколько секунд показывать видео; 0 — до нажатия Enter; по умолчанию — до конца видео",
    )
    args = parser.parse_args()
    if args.view_seconds is not None and args.view_seconds < 0:
        parser.error("--view-seconds must be >= 0")
    return args


def _upload_and_preview(server_url, video_path, company_name, view_seconds):
    # Сбой загрузки или просмотра не должен останавливать обработку остальных строк.
    try:
        video_url = video_client.UploadVideo(server_url, video_path, company_name)
    except Exception as exc:
        print(f"UploadVideo failed for {company_name!r}: {exc}", file=sys.stderr)
        return
    print(f"Uploaded {company_name!r}: {video_url}")
    try:
        video_client.PreviewVideo(video_url, view_seconds)
    except Exception as exc:
        print(f"Preview failed for {company_name!r}: {exc}", file=sys.stderr)


def main():
    args = _parse_args()
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
                if args.server:
                    _upload_and_preview(args.server, path, row["Company Name"], args.view_seconds)


if __name__ == "__main__":
    main()

import hashlib


def calculate_checksum(path):

    sha256 = hashlib.sha256()


    with open(path, "rb") as file:

        while chunk := file.read(1024 * 1024):

            sha256.update(chunk)


    return sha256.hexdigest()
from cryptography.fernet import Fernet
import os

if __name__ == "__main__":
    key = Fernet.generate_key()
    with open('/etc/atm/key', 'wb') as f:
        f.write(key)
    print("Chave gerada e salva em /etc/atm/key. Mantenha-a segura!")

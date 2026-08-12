"""Leitura da rede a partir do config.ini.

`get_network()` decide quais endereços a tela aceita. Um erro aqui não é
cosmético: uma rede errada ou permissiva deixa o cliente enviar dinheiro de
verdade para um destino que o BTCPay vai recusar. Por isso todo caminho de
erro cai em mainnet, o lado seguro.
"""

import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401  — insere src/ no sys.path; precisa vir antes

import atm_core


class TestGetNetwork(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'config.ini')
        p = mock.patch.object(atm_core, '_CONFIG_PATH', self.path)
        p.start()
        self.addCleanup(p.stop)

    def escrever(self, corpo):
        with open(self.path, 'w') as f:
            f.write(corpo)

    def test_le_cada_rede_suportada(self):
        for rede in atm_core.NETWORKS:
            with self.subTest(rede):
                self.escrever(f'[btcpay]\nnetwork = {rede}\n')
                self.assertEqual(atm_core.get_network(), rede)

    def test_ignora_caixa_e_espacos(self):
        self.escrever('[btcpay]\nnetwork =   TestNet  \n')
        self.assertEqual(atm_core.get_network(), 'testnet')

    def test_sem_o_campo_usa_mainnet(self):
        self.escrever('[btcpay]\nhost = https://x\n')
        self.assertEqual(atm_core.get_network(), 'mainnet')

    def test_sem_arquivo_usa_mainnet(self):
        os.remove(self.path) if os.path.exists(self.path) else None
        self.assertEqual(atm_core.get_network(), 'mainnet')

    def test_valor_invalido_usa_mainnet_com_aviso(self):
        """Um erro de digitação não pode virar 'aceita qualquer rede' em
        silêncio — nem derrubar o ATM."""
        self.escrever('[btcpay]\nnetwork = testenet\n')
        with self.assertLogs(level='WARNING') as cm:
            self.assertEqual(atm_core.get_network(), 'mainnet')
        self.assertIn('testenet', ' '.join(cm.output))

    def test_valor_vazio_usa_mainnet(self):
        self.escrever('[btcpay]\nnetwork =\n')
        self.assertEqual(atm_core.get_network(), 'mainnet')

    def test_config_ilegivel_nao_levanta(self):
        """get_network roda no __init__ da janela: uma exceção aqui derrubaria
        o ATM na inicialização."""
        self.escrever('isto não é um ini válido [[[')
        self.assertEqual(atm_core.get_network(), 'mainnet')


if __name__ == '__main__':
    unittest.main()

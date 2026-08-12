"""Validação de endereços Bitcoin, invoices BOLT11 e Lightning Address.

Este é o portão que decide se o dinheiro do cliente sai para um destino
digitado/escaneado errado. Os vetores de endereço são GERADOS por
codificadores de referência (tests/support.py) em vez de digitados de memória.
"""

import hashlib
import unittest

import support

import btc_address as ba


class TestBase58(unittest.TestCase):
    def setUp(self):
        self.h160 = hashlib.sha256(b'atm-test').digest()[:20]

    def test_aceita_versoes_da_rede_configurada(self):
        casos = [
            (0x00, 'mainnet', 'P2PKH mainnet'),
            (0x05, 'mainnet', 'P2SH mainnet'),
            (0x6F, 'testnet', 'P2PKH testnet'),
            (0xC4, 'testnet', 'P2SH testnet'),
            (0x6F, 'regtest', 'P2PKH regtest'),
            (0xC4, 'regtest', 'P2SH regtest'),
        ]
        for version, network, nome in casos:
            with self.subTest(nome):
                addr = support.b58check_encode(version, self.h160)
                self.assertTrue(ba.validate_bitcoin_address(addr, network))

    def test_rejeita_versoes_de_outras_moedas(self):
        """0x30 (Litecoin) e 0x1E (Dogecoin) têm Base58Check válido mas não são
        Bitcoin. Enviar para eles queimaria os fundos."""
        for version in (0x30, 0x1E):
            with self.subTest(hex(version)):
                addr = support.b58check_encode(version, self.h160)
                self.assertFalse(ba.validate_bitcoin_address(addr))

    def test_rejeita_checksum_corrompido(self):
        addr = support.b58check_encode(0x00, self.h160)
        trocado = addr[:-1] + ('2' if addr[-1] != '2' else '3')
        self.assertFalse(ba.validate_bitcoin_address(trocado))

    def test_rejeita_caractere_fora_do_alfabeto(self):
        addr = support.b58check_encode(0x00, self.h160)
        self.assertFalse(ba.validate_bitcoin_address(addr[:-1] + '0'))


class TestSegwit(unittest.TestCase):
    # Vetores do BIP-173/BIP-350.
    P2WPKH = 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4'
    P2WSH_TESTNET = 'tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7'
    TAPROOT = 'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0'
    REGTEST = 'bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080'

    def test_aceita_v0_e_taproot(self):
        casos = [(self.P2WPKH, 'mainnet'), (self.TAPROOT, 'mainnet'),
                 (self.P2WSH_TESTNET, 'testnet'), (self.REGTEST, 'regtest')]
        for addr, network in casos:
            with self.subTest(addr[:12]):
                self.assertTrue(ba.validate_bitcoin_address(addr, network))

    def test_aceita_maiusculas_mas_rejeita_caso_misto(self):
        self.assertTrue(ba.validate_bitcoin_address(self.P2WPKH.upper()))
        misto = self.P2WPKH[:30] + self.P2WPKH[30:].upper()
        self.assertFalse(ba.validate_bitcoin_address(misto))

    def test_rejeita_checksum_corrompido(self):
        self.assertFalse(ba.validate_bitcoin_address(self.P2WPKH[:-1] + '5'))

    def test_rejeita_hrp_desconhecido(self):
        """Um HRP fora de bc/tb/bcrt (ex.: 'ltc') com checksum válido não pode
        passar — é endereço de outra rede."""
        falso = support.bech32_encode('ltc', [0] + [1] * 32)
        self.assertFalse(ba.validate_bitcoin_address(falso))

    def test_teto_de_tamanho_contra_dos(self):
        """Guarda de DoS: entrada gigante não pode chegar ao decode Base58Check
        (O(n^2)) e congelar a GUI do quiosque."""
        self.assertFalse(ba.validate_bitcoin_address('1' * 101))

    def test_rejeita_vazio_e_nao_string(self):
        for valor in ('', '   ', None, 123, b'bc1q'):
            with self.subTest(repr(valor)):
                self.assertFalse(ba.validate_bitcoin_address(valor))


class TestRedeAmarradaAConfig(unittest.TestCase):
    """Um endereço de outra rede é recusado, mesmo com o checksum perfeito.

    Antes, a validação não conhecia a rede: um ATM de mainnet aceitava um
    endereço de testnet escaneado por engano, ficava com o dinheiro do cliente
    e só descobria o problema quando o BTCPay recusasse o envio — a essa
    altura, uma transação presa na fila."""

    ENDERECOS = {
        'mainnet': ['bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4',
                    'bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0'],
        'testnet': ['tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7'],
        'regtest': ['bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080'],
    }

    def setUp(self):
        h160 = hashlib.sha256(b'atm-test').digest()[:20]
        self.ENDERECOS['mainnet'].append(support.b58check_encode(0x00, h160))
        self.ENDERECOS['testnet'].append(support.b58check_encode(0x6F, h160))

    def test_cada_endereco_so_vale_na_propria_rede(self):
        for rede_do_endereco, enderecos in self.ENDERECOS.items():
            for addr in enderecos:
                for rede_do_atm in ba.NETWORKS:
                    # testnet e regtest compartilham os version bytes Base58,
                    # então só os endereços SegWit distinguem as duas.
                    if {rede_do_endereco, rede_do_atm} == {'testnet', 'regtest'} \
                            and not addr.startswith(('tb1', 'bcrt1')):
                        continue
                    esperado = rede_do_endereco == rede_do_atm
                    with self.subTest(endereco=addr[:14], atm=rede_do_atm):
                        self.assertEqual(
                            ba.validate_bitcoin_address(addr, rede_do_atm),
                            esperado)

    def test_padrao_e_mainnet(self):
        """Sem rede informada, recusa endereço de teste: num caixa que opera
        dinheiro de verdade, é o lado seguro para errar."""
        self.assertEqual(ba.DEFAULT_NETWORK, 'mainnet')
        self.assertTrue(ba.validate_bitcoin_address(self.ENDERECOS['mainnet'][0]))
        self.assertFalse(ba.validate_bitcoin_address(self.ENDERECOS['testnet'][0]))

    def test_rede_desconhecida_recusa_tudo(self):
        """Falha fechada: um valor inesperado não pode virar 'aceita qualquer
        rede'."""
        for addr in self.ENDERECOS['mainnet'] + self.ENDERECOS['testnet']:
            with self.subTest(addr[:14]):
                self.assertFalse(ba.validate_bitcoin_address(addr, 'liteneto'))

    def test_invoice_de_outra_rede_e_recusada(self):
        casos = [('lnbc2500u', 'mainnet'), ('lntb20m', 'testnet'),
                 ('lnbcrt500u', 'regtest')]
        for hrp, rede_da_invoice in casos:
            invoice = support.make_invoice(hrp)
            for rede_do_atm in ba.NETWORKS:
                with self.subTest(invoice=hrp, atm=rede_do_atm):
                    self.assertEqual(
                        ba.validate_lightning_invoice(invoice, rede_do_atm),
                        rede_da_invoice == rede_do_atm)

    def test_invoice_de_regtest_nao_passa_por_mainnet(self):
        """Armadilha do prefixo: 'lnbcrt' começa com 'lnbc', então comparar
        com startswith aceitaria uma invoice de regtest num ATM de mainnet."""
        self.assertFalse(ba.validate_lightning_invoice(
            support.make_invoice('lnbcrt500u'), 'mainnet'))


class TestInvoiceBolt11(unittest.TestCase):
    def test_aceita_prefixos_da_rede_configurada(self):
        casos = [('lnbc2500u', 'mainnet'), ('lnbc', 'mainnet'),
                 ('lntb20m', 'testnet'), ('lnbcrt500u', 'regtest')]
        for hrp, network in casos:
            with self.subTest(hrp):
                self.assertTrue(ba.validate_lightning_invoice(
                    support.make_invoice(hrp), network))

    def test_rejeita_checksum_corrompido(self):
        self.assertFalse(ba.validate_lightning_invoice(
            support.corrupt_checksum(support.make_invoice())))

    def test_rejeita_prefixo_desconhecido(self):
        self.assertFalse(ba.validate_lightning_invoice(
            support.bech32_encode('lnxx2500u', [1] * 60)))

    def test_aceita_acima_de_90_caracteres(self):
        """BOLT11 remove o teto de 90 chars do BIP-173; uma invoice real passa
        de 200 caracteres e não pode ser rejeitada por tamanho."""
        longa = support.bech32_encode('lnbc2500u', [1] * 300)
        self.assertGreater(len(longa), 90)
        self.assertTrue(ba.validate_lightning_invoice(longa))

    def test_rejeita_vazio_e_nao_string(self):
        for valor in ('', None, 42):
            with self.subTest(repr(valor)):
                self.assertFalse(ba.validate_lightning_invoice(valor))


class TestLightningAddress(unittest.TestCase):
    def test_aceita_formatos_validos(self):
        for addr in ('a@b.com', 'voce@walletofsatoshi.com',
                     'u_s-e.r@sub.dominio.io', 'A@B.COM'):
            with self.subTest(addr):
                self.assertTrue(ba.validate_lightning_address(addr))

    def test_rejeita_formatos_invalidos(self):
        casos = ['sem-arroba.com', 'a@b', 'a b@c.com', '@dominio.com',
                 'user@', 'a@' + 'x' * 600, '', None]
        for addr in casos:
            with self.subTest(repr(addr)):
                self.assertFalse(ba.validate_lightning_address(addr))

    def test_invoice_nao_e_confundida_com_endereco(self):
        self.assertFalse(ba.validate_lightning_address(support.make_invoice()))


class TestDecodeValorInvoice(unittest.TestCase):
    """O valor decodificado aqui é a única defesa contra um servidor LNURL que
    devolva uma invoice de valor diferente do solicitado."""

    def test_multiplicadores(self):
        casos = [
            ('lnbc1abc', None),            # sem valor (amountless)
            ('lnbc2500u1abc', 250_000_000),
            ('lnbc1m1abc', 100_000_000),
            ('lnbc10n1abc', 1_000),
            ('lnbc10p1abc', 1),
            ('lntb20m1abc', 2_000_000_000),
            ('lnbcrt500u1abc', 50_000_000),
        ]
        for invoice, esperado in casos:
            with self.subTest(invoice):
                self.assertEqual(ba.decode_bolt11_amount_msats(invoice), esperado)

    def test_valor_sem_multiplicador_e_btc_inteiro(self):
        self.assertEqual(ba.decode_bolt11_amount_msats('lnbc31abc'),
                         3 * 100_000_000_000)

    def test_rejeita_fracao_de_msat(self):
        """1p = 0,1 msat: não é um número inteiro de millisatoshi."""
        self.assertIsNone(ba.decode_bolt11_amount_msats('lnbc1p1abc'))
        self.assertIsNone(ba.decode_bolt11_amount_msats('lnbc5p1abc'))

    def test_rejeita_digitos_unicode(self):
        """str.isdigit() aceita '٣'; Decimal() não. Sem o isascii(), uma
        invoice hostil derrubaria a decodificação com InvalidOperation."""
        self.assertIsNone(ba.decode_bolt11_amount_msats('lnbc٣3٣1abc'))

    def test_rejeita_entradas_malformadas(self):
        for invoice in ('lnbc2500x1abc', 'lnxx1abc', 'lnbc', '', None, 7):
            with self.subTest(repr(invoice)):
                self.assertIsNone(ba.decode_bolt11_amount_msats(invoice))

    def test_prefixo_mais_longo_tem_precedencia(self):
        """'lnbcrt' (regtest) começa com 'lnbc'; casar o prefixo curto leria
        'rt500u' como valor."""
        self.assertEqual(ba.decode_bolt11_amount_msats('lnbcrt500u1abc'),
                         50_000_000)


if __name__ == '__main__':
    unittest.main()

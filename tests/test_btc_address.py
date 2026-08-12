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

    def test_aceita_versoes_suportadas(self):
        casos = [
            (0x00, 'P2PKH mainnet'),
            (0x05, 'P2SH mainnet'),
            (0x6F, 'P2PKH testnet'),
            (0xC4, 'P2SH testnet'),
        ]
        for version, nome in casos:
            with self.subTest(nome):
                addr = support.b58check_encode(version, self.h160)
                self.assertTrue(ba.validate_bitcoin_address(addr))

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
        for addr in (self.P2WPKH, self.P2WSH_TESTNET, self.TAPROOT, self.REGTEST):
            with self.subTest(addr[:12]):
                self.assertTrue(ba.validate_bitcoin_address(addr))

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


class TestRedeNaoAmarradaAConfig(unittest.TestCase):
    """FIXA O COMPORTAMENTO ATUAL, que é uma decisão em aberto (achado 6 da
    revisão): endereços de testnet/regtest são aceitos mesmo num ATM de
    mainnet, porque a validação não conhece a rede configurada. A POC roda em
    testnet, então aceitar é intencional HOJE; ao amarrar a rede ao
    config.ini, este teste deve mudar junto — de propósito, não por acidente."""

    def test_testnet_e_regtest_aceitos_sem_verificar_config(self):
        h160 = hashlib.sha256(b'atm-test').digest()[:20]
        self.assertTrue(ba.validate_bitcoin_address(
            support.b58check_encode(0x6F, h160)))
        self.assertTrue(ba.validate_bitcoin_address(
            'bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080'))


class TestInvoiceBolt11(unittest.TestCase):
    def test_aceita_prefixos_conhecidos_com_checksum_valido(self):
        for hrp in ('lnbc2500u', 'lntb20m', 'lnbcrt500u', 'lnbc'):
            with self.subTest(hrp):
                self.assertTrue(
                    ba.validate_lightning_invoice(support.make_invoice(hrp)))

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

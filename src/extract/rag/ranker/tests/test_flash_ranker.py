import unittest
from extract.rag.ranker.flash_ranker import FlashRanker
from extract.rag.models.chunk_vo import ChunkVo, RankedChunkVo
from pipelines.nace.nace_tree import NaceVo, RankedNaceVo


class TestFlashRanker(unittest.TestCase):
    def setUp(self):
        self.sample_chunks_a = [
            ChunkVo(
                id="71fc5b10-8770-46f6-8a22-036c378d8f04",
                celex_id="32003D0570",
                chunk="# Article 1\nOn the basis of the facts in its possession, there are no grounds under Article 81(1) of the Treaty or Article 53(1) of the EEA Agreement for action on the part of the Commission in respect of the provisions of the Agreement between O2 UK Limited and T-Mobile UK Limited dated 20 September 2001 and amended on 9 April 2002 (‘the Agreement’) that relate to site sharing and the exchange of information necessary to permit site sharing and national roaming.\n\n# Article 2\nPursuant to Article 81(3) of the Treaty and Article 53(3) of the EEA Agreement, the provisions of Articles 81(1) of the Treaty and Article 53(1) of the EEA Agreement are declared inapplicable to the provisions of the Agreement that concern national roaming within the residual area of the Initial Build Area as defined in the Parties’ statement of 12 March 2003 from 6 February 2002 until 31 December 2007.\n\n# Article 3\nPursuant to Article 81(3) of the Treaty and Article 53(3) of the EEA Agreement, the provisions of Articles 81(1) of the Treaty and Article 53(1) of the EEA Agreement are declared inapplicable to the provisions of the Agreement that concern national roaming within the Divided Area, from 6 February 2002 until 31 December 2008.\n\n# Article 4\nThis Decision is addressed to:\nO2 UK Limited\n260 Bath Road\nSlough\nBerkshire\nSL1 4DX\nUnited Kingdom\nand\nT-Mobile UK Imperial Place Maxwell Road Borehamwood Hertfordshire WD6 1EA United Kingdom.\nDone at Brussels, 30 April 2003.\nFor the Commission\nMario MONTI\nMember of the Commission",
                origin="test_mock",
                similarity=0.5,
                doc_pos=1
            ),
            ChunkVo(
                id="6f6141f9-0438-4306-ae42-e42d95fcc40b",
                celex_id="32003D0570",
                chunk="# 2.1.1. INTRODUCTION\n(40) Markets can generally be divided into wholesale and retail markets. In telecommunications, wholesale markets typically consist of the provision of access to networks or network elements and of network services to operators of networks and services. Retail markets consist of the provision of communications services to end-users (24). Within these broad categories, narrower markets can be defined not only on the basis of the characteristics of the service concerned and the degree to which it can be substituted by other services based on price, usage and consumer preference, but by an analysis of competitive conditions and the structure of demand and supply (25). It is evident given the advantages of mobility and the premium paid for it that mobile services are in general not substitutable by fixed services. Mobile and fixed voice services are therefore part of different services markets, as has been determined in a number of Commission decisions (26).\n(41) The network access and services markets that are primarily concerned by the Agreement are:\n(a) the market for sites and site infrastructure for digital mobile radiocommunications equipment;\n(b) the market for wholesale access to national roaming for 3G communications services.\n(42) In addition, the markets for wholesale access to 3G services, as well as the downstream retail markets for 3G services are affected indirectly.\n2.1.2. WHOLESALE MOBILE NETWORK ACCESS MARKETS\n(43) Access to physical facilities, such as sites, and site infrastructure, that is to say masts and antennae, as well as ducts, leased lines and rights of way, that serve as part of a mobile telecommunications network infrastructure, may constitute access to particular mobile network markets. In addition, there are wholesale network access and services markets for the provision of digital mobile communications services to other operators. These can be divided broadly into two categories:\n(a) wholesale network services related to interconnection that allow communication to take place between the users of different networks;\n(b) wholesale access services that relate to the use of a host or visited network by customers of other operators.",
                origin="test_mock",
                similarity=0.5,
                doc_pos=1
            )
        ]

        self.sample_chunks_b = [
            ChunkVo(
                id="c3197807-b41b-406d-8249-ef65022f81f5",
                celex_id="32003D0570",
                similarity=0.5,
                chunk=r"# Product/service markets\ndifference between the two forms of access is that an operator relying on national roaming can itself determine the range of services available to its subscribers, and can provide services that are not available to customers of the host network. A service provider, however, can only provide simple resale of the range of services offered by the network operator that is providing it with wholesale airtime.\n(58) Another possible wholesale market is the market for call origination services where providers of carrier selection services purchase the right to obtain access to mobile networks in order to originate calls that they terminate under their own responsibility. It is possible that, in addition, new forms of wholesale access to 3G networks and services may develop and come to constitute separate relevant markets.\n(57) There are a number of other possible wholesale markets for 3G network services and network access that may be affected by the Agreement, such as the market for the provision of wholesale airtime access to service providers, which has existed in the United Kingdom on the basis of regulatory obligations. Wholesale airtime access is similar to national roaming in that it also concerns the wholesale provision of network access and minutes (airtime) by a host network. It has been supplied to service providers by licensed mobile operators in the United Kingdom as a condition of their licences $\left( ^ { 3 0 } \right)$ . The (59) Wholesale 3G network services and network access are likely to be distinct from network services and access for 2G or 2,5G services, because the range of services that can be provided based on 3G networks is broader and different, given the availability of significantly higher transmission speeds. However given the degree of development of 3G wholesale markets, which are still emerging, it is too early to describe in detail the demand side for network services and access on commercial terms in such markets, with the exception of demand from service providers for access to wholesale airtime. From a supply side these markets are logically limited to 3G network operators and to any other parties that may obtain a right to provide the relevant degree of access to 3G networks.\n\n# Geographic market\n(60) Given national licensing and pricing patterns, the geographic scope of such wholesale markets is likely to be national $\binom { 3 1 } { }$ . For the purposes of the present Decision it is not necessary to define those markets more closely. Their definition will therefore be left open.",
                origin="test_mock",
                doc_pos=1
            ),
            ChunkVo(
                id="225574cc-4c2c-486b-97e9-b0643f03074b",
                celex_id="32003D0570",
                similarity=0.5,
                chunk=r"# 2.1.4.2. Potentially affected retail markets\n(61) Whereas the cooperation covered by the Agreement is limited to site sharing and national roaming at wholesale network level, the effects of this cooperation could be felt in the downstream retail services markets where the Parties are active independently of each other. Within the area of mobile retail services, voice and data services have so far been offered in a bundled manner, suggesting that they may be part of the same market. The 3G network operators are likely to offer ‘seamless’ 2G and 3G voice and data services by providing both types of services on a single SIM card. However, the balance between voice and data services is expected to shift fundamentally: whereas 2G data services are largely limited to fax and SMS, and voice services typically account for over $90 \%$ of 2G mobile operators’ revenues, for 3G networks, with services like teleshopping, video telephony and video conferencing, it is expected that eventually between $50 \%$ and two thirds of revenue may be generated by data services. It is therefore useful to analyse digital mobile voice telephony services and digital mobile data services separately. At least initially this distinction largely corresponds with that between circuit-switched and packet-switched services.\nDigital mobile voice services (62) Concerning mobile voice telephony markets the Commission has so far generally not distinguished between different technologies. Most decisions have determined that both analogue and digital GSM 900 and 1800 are part of the same mobile voice telephony market, while testing narrower market definitions to ensure that no dominant positions arose on any market definition $\binom { 3 2 } { }$ . However, as analogue mobile telephony has been phased out by the operators in the United Kingdom, the services concerned by the\nAgreement are digital mobile voice telephony. So far the Commission has not defined different markets for 2G, 2,5G and 3G retail services $\binom { 3 3 } { }$ .",
                origin="test_mock",
                doc_pos=1
            ),
            # Intentional duplicate from list A for dedup testing
            ChunkVo(
                id="6f6141f9-0438-4306-ae42-e42d95fcc40b",
                celex_id="32003D0570",
                similarity=0.5,
                chunk="# 2.1.1. INTRODUCTION\n(40) Markets can generally be divided into wholesale and retail markets. In telecommunications, wholesale markets typically consist of the provision of access to networks or network elements and of network services to operators of networks and services. Retail markets consist of the provision of communications services to end-users (24). Within these broad categories, narrower markets can be defined not only on the basis of the characteristics of the service concerned and the degree to which it can be substituted by other services based on price, usage and consumer preference, but by an analysis of competitive conditions and the structure of demand and supply (25). It is evident given the advantages of mobility and the premium paid for it that mobile services are in general not substitutable by fixed services. Mobile and fixed voice services are therefore part of different services markets, as has been determined in a number of Commission decisions (26).\n(41) The network access and services markets that are primarily concerned by the Agreement are:\n(a) the market for sites and site infrastructure for digital mobile radiocommunications equipment;\n(b) the market for wholesale access to national roaming for 3G communications services.\n(42) In addition, the markets for wholesale access to 3G services, as well as the downstream retail markets for 3G services are affected indirectly.\n2.1.2. WHOLESALE MOBILE NETWORK ACCESS MARKETS\n(43) Access to physical facilities, such as sites, and site infrastructure, that is to say masts and antennae, as well as ducts, leased lines and rights of way, that serve as part of a mobile telecommunications network infrastructure, may constitute access to particular mobile network markets. In addition, there are wholesale network access and services markets for the provision of digital mobile communications services to other operators. These can be divided broadly into two categories:\n(a) wholesale network services related to interconnection that allow communication to take place between the users of different networks;\n(b) wholesale access services that relate to the use of a host or visited network by customers of other operators.",
                origin="test_mock",
                doc_pos=1
            )
        ]

        self.ranker = FlashRanker()

    def test_ranking_and_top_k(self):
        """Ensure ranking happens successfully and results respect top_k."""
        # Query related to wholesale access and services markets
        query = "What are the wholesale network access and services markets that allow communication to take place between users of different networks?"
        # Should pass combined list since deduplication was moved to orchestrator in reality, 
        # but for flash ranker isolated testing we just provide the raw target array
        combined = self.sample_chunks_a + self.sample_chunks_b
        
        # We must manuall dedup in test so the ranker gets a clean list as expected from orchestrator
        seen = set()
        deduped = []
        for c in combined:
            if c.id not in seen:
                seen.add(c.id)
                deduped.append(c)

        results = self.ranker.rank_chunkvo(query, deduped, top_k=2)
        
        # Should only return the top 2
        self.assertEqual(len(results), 2)
        
        # Ensure they are safely marshalled into Pydantic RankedDocumentChunk
        for result in results:
            self.assertIsInstance(result, RankedChunkVo)
            self.assertTrue(hasattr(result, "score"))
            self.assertIsInstance(result.score, float)
        
        # We expect 6f6141f9-0438-4306-ae42-e42d95fcc40b to be highly ranked for this query due to exact wording
        self.assertTrue(any(res.id == "6f6141f9-0438-4306-ae42-e42d95fcc40b" for res in results))

    def setUp_nace_nodes(self) -> list[NaceVo]:
        """Return a small set of NACE group/class nodes for reranker tests."""
        return [
            NaceVo(type="group", code="26.1", title="Manufacture of electronic components and boards", isic_code="261", childs=[]),
            NaceVo(type="group", code="45.1", title="Sale of motor vehicles", isic_code="451", childs=[]),
            NaceVo(type="group", code="46.7", title="Wholesale of other machinery, equipment and supplies", isic_code="467", childs=[]),
            NaceVo(type="group", code="62.0", title="Computer programming, consultancy and related activities", isic_code="620", childs=[]),
            NaceVo(type="group", code="35.1", title="Electric power generation, transmission and distribution", isic_code="351", childs=[]),
        ]

    def test_rank_chunkvo_returns_ranked_chunk_vo_instances(self):
        """All items returned by rank_chunkvo must be RankedChunkVo instances with a float score."""
        query = "national roaming wholesale access"
        results = self.ranker.rank_chunkvo(query, self.sample_chunks_a)

        self.assertEqual(len(results), len(self.sample_chunks_a))
        for result in results:
            self.assertIsInstance(result, RankedChunkVo)
            self.assertIsInstance(result.score, float)
            self.assertIsInstance(result.base_score, float)

    def test_rank_chunkvo_empty_input(self):
        """rank_chunkvo must return an empty list when given no chunks."""
        results = self.ranker.rank_chunkvo("any query", [])
        self.assertEqual(results, [])

    def test_rank_chunkvo_top_k_respected(self):
        """rank_chunkvo must return at most top_k results."""
        query = "market definition mobile services"
        combined = self.sample_chunks_a + self.sample_chunks_b
        results = self.ranker.rank_chunkvo(query, combined, top_k=1)
        self.assertEqual(len(results), 1)

    def test_rank_chunkvo_scores_descending(self):
        """Results must be sorted from highest to lowest score."""
        query = "site sharing agreement between mobile operators"
        combined = self.sample_chunks_a + self.sample_chunks_b
        results = self.ranker.rank_chunkvo(query, combined)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rank_chunkvo_base_score_not_exceeds_adjusted(self):
        """base_score must never exceed the adjusted score (position boost only adds)."""
        query = "Article 1 grounds for Commission action"
        results = self.ranker.rank_chunkvo(query, self.sample_chunks_a)
        for result in results:
            self.assertLessEqual(result.base_score, result.score)

    def test_rank_chunkvo_disable_position_boost_uses_base_score(self):
        """apply_position_boost=False must collapse score to base_score exactly.

        This is the enumeration / recall mode used by EnumerationSearch: the
        Legal Climax depth boost must NOT bias rare early-document mentions
        downward.
        """
        query = "Article 1 grounds for Commission action"
        combined = self.sample_chunks_a + self.sample_chunks_b
        results = self.ranker.rank_chunkvo(
            query, combined, apply_position_boost=False
        )
        self.assertTrue(results)
        for result in results:
            self.assertEqual(
                result.score,
                result.base_score,
                "With boost disabled, score must equal base_score for every chunk",
            )


    def test_rank_nacevo_returns_ranked_nace_vo_instances(self):
        """All items returned by rank_nacevo must be RankedNaceVo with a float score."""
        nodes = self.setUp_nace_nodes()
        results = self.ranker.rank_nacevo("software development and IT consulting", nodes)

        self.assertEqual(len(results), len(nodes))
        for result in results:
            self.assertIsInstance(result, RankedNaceVo)
            self.assertIsInstance(result.score, float)
            self.assertIsInstance(result.base_score, float)

    def test_rank_nacevo_empty_input(self):
        """rank_nacevo must return an empty list when given no nodes."""
        results = self.ranker.rank_nacevo("any query", [])
        self.assertEqual(results, [])

    def test_rank_nacevo_top_k_respected(self):
        """rank_nacevo must return at most top_k results."""
        nodes = self.setUp_nace_nodes()
        results = self.ranker.rank_nacevo("electricity grid infrastructure", nodes, top_k=2)
        self.assertEqual(len(results), 2)

    def test_rank_nacevo_scores_descending(self):
        """Results must be sorted from highest to lowest score."""
        nodes = self.setUp_nace_nodes()
        results = self.ranker.rank_nacevo("sale and distribution of automobiles", nodes)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rank_nacevo_best_match_plausible(self):
        """The top result for a motor vehicle query should be the motor vehicle node."""
        nodes = self.setUp_nace_nodes()
        results = self.ranker.rank_nacevo("retail sale of cars and motor vehicles", nodes)
        self.assertEqual(results[0].code, "45.1")

    def test_rank_nacevo_preserves_vo_reference(self):
        """Each RankedNaceVo must reference the original NaceVo object."""
        nodes = self.setUp_nace_nodes()
        node_map = {vo.code: vo for vo in nodes}
        results = self.ranker.rank_nacevo("electronic components manufacturing", nodes)
        for result in results:
            self.assertIs(result.vo, node_map[result.code])


if __name__ == '__main__':
    unittest.main()

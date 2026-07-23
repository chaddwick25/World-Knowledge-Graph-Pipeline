"""
Cross-Attention IGEA Model

BiLSTM Cross-Attention Neural Architecture for entity alignment.
Per Dsouza et al., ISWC 2023 (https://arxiv.org/abs/2105.00847)

This replaces basic cosine similarity with advanced neural cross-attention
for learning cross-cultural semantic variations in entity names.
"""
#TODO: Review this code
import logging
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional

logger = logging.getLogger(__name__)
class CrossAttentionIGEA(nn.Module):
    """
    BiLSTM Cross-Attention Neural Architecture mapping explicitly to `alishiba14/IGEA`.
    
    Replaces basic baseline cosine similarities with advanced neighbourhood spatial sequencing.
    
    Architecture:
        1. BiLSTM encoding of OSM and Wikidata name sequences
        2. Cross-attention mechanism (OSM attends to Wikidata)
        3. Mean pooling of attended representations
        4. Binary classifier (aligned / not aligned)
    
    Args:
        emb_dim: Embedding dimension (default 300 for FastText)
        hidden_dim: LSTM hidden dimension (default 150)
    """
    
    def __init__(self, emb_dim=300, hidden_dim=150):
        super(CrossAttentionIGEA, self).__init__()
        self.bilstm = nn.LSTM(
            input_size=emb_dim,
            hidden_size=hidden_dim,
            num_layers=1,
            batch_first=True,
            bidirectional=True
        )
        self.attention_weights = nn.Linear(hidden_dim * 2, hidden_dim * 2, bias=False)
        self.classifier = nn.Sequential(
            nn.Linear((hidden_dim * 2) * 2, 64),
            nn.ReLU(),
            nn.Linear(64, 1),
            nn.Sigmoid()
        )
    
    def forward(self, osm_seq, wd_seq):
        """
        Forward pass through cross-attention network.
        
        Args:
            osm_seq: OSM name sequence embeddings (batch, seq_len, emb_dim)
            wd_seq: Wikidata name sequence embeddings (batch, seq_len, emb_dim)
        
        Returns:
            Alignment probability (batch,) in [0, 1]
        """
        # BiLSTM encoding
        # osm_seq, wd_seq shape: (batch, seq_len, emb_dim)
        osm_context, _ = self.bilstm(osm_seq)
        wd_context, _ = self.bilstm(wd_seq)
        
        # Cross attention
        # A = softmax(osm_context * W * wd_context^T)
        osm_proj = self.attention_weights(osm_context)
        attn_scores = torch.bmm(osm_proj, wd_context.transpose(1, 2))
        attn_weights = F.softmax(attn_scores, dim=-1)
        
        # wd_attended shape: (batch, osm_seq_len, hidden_dim * 2)
        wd_attended = torch.bmm(attn_weights, wd_context)
        
        # Pool sequences (mean pooling)
        osm_pooled = torch.mean(osm_context, dim=1)
        wd_pooled = torch.mean(wd_attended, dim=1)
        
        # Concatenate and classify
        combined = torch.cat([osm_pooled, wd_pooled], dim=1)
        return self.classifier(combined).squeeze()
    
    def load_weights(self, weights_path: str, device: Optional[torch.device] = None):
        """
        Load pre-trained model weights.
        
        Args:
            weights_path: Path to .pt weights file
            device: Target device (default: CPU)
        """
        if device is None:
            device = torch.device('cpu')
        
        try:
            self.load_state_dict(torch.load(weights_path, map_location=device))
            self.eval()
            logger.info(f"CrossAttentionIGEA: Loaded weights from {weights_path}")
        except Exception as e:
            logger.error(f"CrossAttentionIGEA: Failed to load weights: {e}")
            raise
    
    def save_weights(self, weights_path: str):
        """
        Save model weights.
        
        Args:
            weights_path: Path to save .pt weights file
        """
        try:
            torch.save(self.state_dict(), weights_path)
            logger.info(f"CrossAttentionIGEA: Saved weights to {weights_path}")
        except Exception as e:
            logger.error(f"CrossAttentionIGEA: Failed to save weights: {e}")
            raise

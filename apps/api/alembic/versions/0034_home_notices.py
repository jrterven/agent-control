"""Durable daily Home notices and source check cadence."""
from alembic import op
import sqlalchemy as sa

revision = '0034_home_notices'
down_revision = '0033_personal_home'


def upgrade():
    op.add_column('home_preferences', sa.Column('last_proactive_at', sa.DateTime(timezone=True)))
    op.add_column('home_panel_snapshots', sa.Column('checked_at', sa.DateTime(timezone=True)))
    op.create_table('home_notices', sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('owner_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('suggestion_id', sa.String(36), sa.ForeignKey('home_suggestions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('day', sa.String(10), nullable=False), sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('owner_id', 'suggestion_id', 'day', 'kind', name='uq_home_notice_daily'))
    op.create_index('ix_home_notices_owner_id', 'home_notices', ['owner_id'])
    op.create_index('ix_home_notices_suggestion_id', 'home_notices', ['suggestion_id'])


def downgrade():
    op.drop_column('home_preferences', 'last_proactive_at')
    op.drop_table('home_notices')
    op.drop_column('home_panel_snapshots', 'checked_at')

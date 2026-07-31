"""Initial tables for CourseIndexState and FailedIngestion.

These models were declared without migrations, so their tables never existed —
`SHOW TABLES LIKE 'coursemate%'` returned nothing on a stack that had been
serving 231 indexed chunks. The index was populated by `coursemate_reindex`,
which calls `send_leaves` directly and touches neither model, so nothing ever
exercised them and nothing failed.

That is what made it invisible. `FailedIngestion` exists precisely so a block
that fails to index is *detectable* rather than a silent gap; without its table,
the first real failure would have raised inside the retry handler and the gap
would have been exactly as silent as if the table were never designed. The sweep
surfaced it because it is the first code that reads these tables back.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='CourseIndexState',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_id', models.CharField(db_index=True, max_length=255, unique=True)),
                ('block_count', models.PositiveIntegerField(default=0)),
                ('blocks_indexed', models.PositiveIntegerField(default=0)),
                ('last_usage_key', models.CharField(blank=True, default='', max_length=255)),
                ('last_indexed_at', models.DateTimeField(blank=True, null=True)),
                ('last_error', models.TextField(blank=True, default='')),
            ],
        ),
        migrations.CreateModel(
            name='FailedIngestion',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('course_id', models.CharField(db_index=True, max_length=255)),
                ('usage_key', models.CharField(db_index=True, max_length=255)),
                ('version', models.CharField(blank=True, default='', max_length=255)),
                ('error', models.TextField(blank=True, default='')),
                ('attempts', models.PositiveSmallIntegerField(default=1)),
                ('first_failed_at', models.DateTimeField(auto_now_add=True)),
                ('last_failed_at', models.DateTimeField(auto_now=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'unique_together': {('course_id', 'usage_key')},
            },
        ),
    ]

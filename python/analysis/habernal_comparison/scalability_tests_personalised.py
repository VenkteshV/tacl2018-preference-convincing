from tests import TestRunner

if __name__ == '__main__':

    acc = 1.0
    dataset_increment = 0

    # Classifications task
    datasets = ['UKPConvArgCrowdSample_evalMACE']

    # Create a plot for the runtime/accuracy against M + include other methods with ling + Glove features
    methods = ['SinglePrefGP_noOpt_weaksprior', 'SVM', 'BI-LSTM', 'PersPrefGP_commonmean_noOpt', ]
    feature_types = ['both'] # 'both'
    embeddings_types = ['word_mean']

    runner = TestRunner('crowdsourcing_argumentation_expts', datasets, feature_types, embeddings_types, methods,
                        dataset_increment)
    runner.run_test_set()